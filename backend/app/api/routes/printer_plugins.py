"""Discover the versioned internal printer addon API and installed adapters.

Printer actions continue through the scoped /printers routes. Installed addons
use backend.app.printers.api for the same underlying lifecycle and operations.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from backend.app.core.auth import RequirePermissionIfAuthEnabled, RequirePrinterPermissionIfAuthEnabled, is_auth_enabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.printers.contracts import UnknownPrinterPlugin
from backend.app.printers.management import PluginManager
from backend.app.printers.packages import MAX_PACKAGE_BYTES
from backend.app.printers.registry import descriptors, get_plugin, plugin_for


class BoundedPluginRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method != "POST" or not request.url.path.endswith("/printer-plugins/packages"):
                return await handler(request)
            maximum = MAX_PACKAGE_BYTES + 1024 * 1024  # Multipart field/header overhead.
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > maximum:
                        raise HTTPException(413, "Printer package request exceeds the upload limit")
                except ValueError as exc:
                    raise HTTPException(400, "Invalid Content-Length") from exc
            received = 0
            exceeded = False

            async def receive():
                nonlocal received, exceeded
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > maximum:
                    exceeded = True
                    # MultipartParser closes its spooled files for this error.
                    raise MultiPartException("Printer package request exceeds the upload limit")
                return message

            try:
                return await handler(Request(request.scope, receive=receive))
            except StarletteHTTPException as exc:
                if exceeded:
                    raise HTTPException(413, "Printer package request exceeds the upload limit") from exc
                raise

        return bounded


router = APIRouter(prefix="/printer-plugins", tags=["printer-plugins"], route_class=BoundedPluginRoute)


async def _plugin_administrator(
    user: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    if await is_auth_enabled(db) and (user is None or not user.is_admin):
        raise HTTPException(403, "Only an administrator can manage executable printer plugins")
    return user


def _registry():
    from backend.app.printers.registry import registry

    registry.load()
    return registry


async def _configured(db: AsyncSession, plugin_id: str) -> list[Printer]:
    return list((await db.scalars(select(Printer).where(Printer.plugin_id == plugin_id))).all())


async def _validate_saved_configuration(db: AsyncSession, plugin_id: str, version: str):
    from backend.app.printers.sdk import SDKValidationError, validate_value

    manifest = await asyncio.to_thread(PluginManager().selection_manifest, plugin_id, version)
    for printer in await _configured(db, plugin_id):
        try:
            validate_value(
                printer.plugin_options or {}, manifest.configuration_schema, path=f"printer[{printer.id}].options"
            )
        except SDKValidationError as exc:
            raise HTTPException(
                409, f"Saved printer settings are incompatible with this plugin version: {exc}"
            ) from exc


async def _apply_selection(db: AsyncSession, plugin_id: str, version: str | None, *, rollback: bool = False):
    from copy import deepcopy

    from backend.app.models.printer_operation import PrinterOperation
    from backend.app.printers.contracts import PrinterConfig, PrinterPluginError
    from backend.app.printers.lifecycle import lifecycle
    from backend.app.printers.management import printer_pin_identity
    from backend.app.printers.rolling import rollouts
    from backend.app.services.printer_manager import printer_manager

    async with lifecycle.change_lock(plugin_id):
        registry = _registry()
        manager = PluginManager()
        target = manager.rollback_target(plugin_id, version) if rollback else version
        if target is None:
            raise HTTPException(400, "A printer plugin version is required")
        await _validate_saved_configuration(db, plugin_id, target)
        try:
            candidate = await asyncio.to_thread(registry.prepare, plugin_id, target)
        except Exception as exc:
            logging.exception("Printer plugin %s candidate %s failed to load", plugin_id, target)
            raise HTTPException(409, f"Printer plugin candidate could not be loaded ({type(exc).__name__})") from exc
        printers = await _configured(db, plugin_id)
        configs = [PrinterConfig.from_printer(printer) for printer in printers]
        for config in configs:
            try:
                candidate.plugin.validate_config(config)
            except (PrinterPluginError, ValueError, TypeError) as exc:
                raise HTTPException(
                    409, f"Saved settings for printer {config.printer_id} are incompatible with this plugin version"
                ) from exc

        # Persist the actual connection provider before changing the default.
        # Restart/recovery must retain the same observer for an unfinished job.
        for config in configs:
            if config.printer_id is None:
                continue
            client = printer_manager.get_client(config.printer_id)
            if client is not None:
                plugin = getattr(client, "_plugin", None)
                if plugin is None:
                    plugin = registry.provider_for(config)
                registry.pin_printer(config, plugin)

        if configs:
            by_id = {config.printer_id: config for config in configs}
            pending = list(
                (
                    await db.scalars(
                        select(PrinterOperation).where(
                            PrinterOperation.printer_id.in_(by_id),
                            PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")),
                        )
                    )
                ).all()
            )
            for operation in pending:
                config = by_id[operation.printer_id]
                schema = deepcopy(operation.operation_schema)
                extensions = schema.setdefault("extensions", {})
                binding = extensions.get("bambuddy:provider")
                if binding is None:
                    try:
                        provider = registry.provider_for(config)
                    except PrinterPluginError:
                        continue  # An unavailable historical origin cannot be invented.
                    runtime = registry.runtime_for(provider)
                    binding = {
                        "plugin_id": plugin_id,
                        "version": runtime.version,
                        "identity": printer_pin_identity(config),
                    }
                    extensions["bambuddy:provider"] = binding
                    operation.operation_schema = schema
                if (
                    isinstance(binding, dict)
                    and binding.get("plugin_id") == plugin_id
                    and binding.get("identity") == printer_pin_identity(config)
                    and isinstance(binding.get("version"), str)
                ):
                    runtime = registry.prepare(plugin_id, binding["version"])
                    registry.retain_runtime(
                        runtime.plugin,
                        f"operation:{operation.id}",
                        printer_id=operation.printer_id,
                        reason="pending operation",
                    )
            await db.commit()

        previous_plugin = registry._plugins.get(plugin_id)
        previous = registry.runtime_for(previous_plugin) if previous_plugin is not None else None
        checkpoint = manager.selection_checkpoint(plugin_id)
        try:
            started_services = await lifecycle.prepare(candidate)
        except Exception as exc:
            logging.exception("Printer plugin %s candidate %s services failed to start", plugin_id, target)
            raise HTTPException(409, f"Printer plugin services could not start ({type(exc).__name__})") from exc
        selected = False
        try:
            result = await asyncio.to_thread(
                manager.select, plugin_id, target, loaded_version=previous.version if previous else None, live=True
            )
            selected = True
            registry.activate(candidate)
            await asyncio.to_thread(manager.record_load, plugin_id, target)
            await rollouts.request(plugin_id, candidate)
        except Exception:
            if previous is not None:
                registry.activate(previous)
            else:
                registry._plugins.pop(plugin_id, None)
                registry.loaded_sources.pop(plugin_id, None)
            if selected:
                await asyncio.to_thread(manager.restore_selection, plugin_id, checkpoint, expected_version=target)
            if started_services:
                await lifecycle.discard(candidate)
            raise
        result.update(activation="rolling", restart_required=False, rollout=rollouts.describe(plugin_id))
        return result


async def _require_unconfigured(db: AsyncSession, plugin_id: str):
    if await _configured(db, plugin_id):
        raise HTTPException(409, "Remove or reassign configured printers before disabling or removing their plugin")


class PluginVersionSelection(BaseModel):
    version: str = Field(min_length=1, max_length=128)


class PluginRollbackSelection(BaseModel):
    version: str | None = Field(default=None, max_length=128)


class PublisherKey(BaseModel):
    key_id: str = Field(min_length=1, max_length=128)
    publisher_id: str = Field(min_length=1, max_length=128)
    public_key: str = Field(min_length=1, max_length=256)


@router.get("")
async def list_printer_plugins(
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    """Installed adapters, models, capabilities and supported command names."""
    return descriptors()


@router.get("/printers/{printer_id}")
async def get_printer_plugin_capabilities(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if printer is None:
        raise HTTPException(404, "Printer not found")
    try:
        plugin = plugin_for(printer)
    except UnknownPrinterPlugin as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "api_version": plugin.api_version,
        "printer_id": printer_id,
        "plugin_id": plugin.id,
        "model": printer.model,
        "capabilities": sorted(plugin.model_capabilities(printer.model)),
        "operations": list(plugin.operations),
    }


@router.get("/management")
async def plugin_management_status(
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    rows = (await db.execute(select(Printer.plugin_id, func.count()).group_by(Printer.plugin_id))).all()
    return PluginManager().describe(_registry(), dict(rows))


@router.post("/packages")
async def stage_printer_package(
    file: UploadFile = File(...),
    expected_sha256: str | None = Form(None),
    _=Depends(_plugin_administrator),
):
    registry = _registry()  # Capture the running selection before staging any new code.
    chunks = []
    size = 0
    try:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_PACKAGE_BYTES:
                raise HTTPException(413, "Printer package exceeds the 64 MiB upload limit")
            chunks.append(chunk)
    finally:
        await file.close()
    unmanaged = {
        plugin_id for plugin_id, source in registry.loaded_sources.items() if source["source"] == "entry-point"
    }
    return await asyncio.to_thread(
        PluginManager().stage,
        b"".join(chunks),
        expected_sha256=expected_sha256,
        unmanaged_ids=unmanaged,
    )


@router.post("/trust-keys")
async def trust_printer_publisher(body: PublisherKey, _=Depends(_plugin_administrator)):
    return await asyncio.to_thread(PluginManager().trust_key, body.key_id, body.publisher_id, body.public_key)


@router.delete("/trust-keys/{key_id}")
async def revoke_printer_publisher(key_id: str, _=Depends(_plugin_administrator)):
    return await asyncio.to_thread(PluginManager().revoke_key, key_id)


@router.post("/{plugin_id}/activate")
async def activate_printer_package(
    plugin_id: str,
    body: PluginVersionSelection,
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await _apply_selection(db, plugin_id, body.version)


@router.post("/{plugin_id}/rollback")
async def rollback_printer_package(
    plugin_id: str,
    body: PluginRollbackSelection | None = None,
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return await _apply_selection(db, plugin_id, body.version if body else None, rollback=True)


@router.post("/{plugin_id}/enable")
async def enable_printer_plugin(
    plugin_id: str,
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    registry = _registry()
    record = PluginManager().read_state()["plugins"].get(plugin_id, {})
    version = record.get("desired_version") or ("bundled" if plugin_id in registry.bundled_versions else None)
    if version:
        await _validate_saved_configuration(db, plugin_id, version)
    return await asyncio.to_thread(PluginManager().set_enabled, plugin_id, True, loaded=plugin_id in registry._plugins)


@router.post("/{plugin_id}/disable")
async def disable_printer_plugin(
    plugin_id: str,
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    from backend.app.printers.lifecycle import lifecycle

    async with lifecycle.change_lock(plugin_id):
        registry = _registry()
        await _require_unconfigured(db, plugin_id)
        if any(item["printer_ids"] for item in registry.running_versions(plugin_id)):
            raise HTTPException(409, "A running printer still uses this plugin")
        return await asyncio.to_thread(
            PluginManager().set_enabled, plugin_id, False, loaded=plugin_id in registry._plugins
        )


@router.delete("/{plugin_id}/versions/{version}")
async def remove_printer_package_version(plugin_id: str, version: str, _=Depends(_plugin_administrator)):
    from backend.app.printers.lifecycle import lifecycle

    async with lifecycle.change_lock(plugin_id):
        registry = _registry()
        loaded_versions = {
            item.get("version_key", item["version"]) for item in registry.running_versions(plugin_id)
        } | lifecycle.protected_versions(plugin_id)
        return await asyncio.to_thread(
            PluginManager().remove_version, plugin_id, version, loaded_versions=loaded_versions
        )


@router.delete("/{plugin_id}")
async def remove_printer_plugin(
    plugin_id: str,
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    from backend.app.printers.lifecycle import lifecycle

    async with lifecycle.change_lock(plugin_id):
        registry = _registry()
        await _require_unconfigured(db, plugin_id)
        if any(item["printer_ids"] for item in registry.running_versions(plugin_id)):
            raise HTTPException(409, "A running printer still uses this plugin")
        return await asyncio.to_thread(PluginManager().remove_plugin, plugin_id)


@router.get("/{plugin_id}")
async def get_printer_plugin(
    plugin_id: str,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    try:
        return get_plugin(plugin_id).descriptor()
    except UnknownPrinterPlugin as exc:
        raise HTTPException(404, str(exc)) from exc

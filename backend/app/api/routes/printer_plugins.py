"""Discover the versioned internal printer addon API and installed adapters.

Printer actions continue through the scoped /printers routes. Installed addons
use backend.app.printers.api for the same underlying lifecycle and operations.
"""

import asyncio
import inspect

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


async def _require_idle(db: AsyncSession, plugin_id: str, *, require_unconfigured: bool = False):
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_operation import PrinterOperation
    from backend.app.printers.contracts import PrinterConfig
    from backend.app.printers.firmware import upload_in_progress
    from backend.app.services.print_scheduler import scheduler as print_scheduler
    from backend.app.services.printer_manager import printer_manager

    printers = await _configured(db, plugin_id)
    if require_unconfigured and printers:
        raise HTTPException(409, "Remove or reassign configured printers before disabling or removing their plugin")
    ids = {printer.id for printer in printers}
    loaded_plugin = _registry()._plugins.get(plugin_id)
    for printer in printers:
        state = printer_manager.get_status(printer.id)
        if state is not None and state.state in printer_manager.ACTIVE_PRINT_STATES:
            raise HTTPException(409, "An affected printer has an active print; wait before changing its plugin")
        if loaded_plugin is not None and upload_in_progress(printer.id, plugin_id=plugin_id):
            raise HTTPException(409, "An affected printer is uploading firmware")
        connection = printer_manager.get_client(printer.id)
        predicate = getattr(loaded_plugin, "can_deactivate", None)
        if connection is not None and predicate is not None:
            ready = predicate(PrinterConfig.from_printer(printer), connection)
            if inspect.isawaitable(ready):
                try:
                    ready = await asyncio.wait_for(ready, timeout=5)
                except TimeoutError as exc:
                    raise HTTPException(409, "The printer plugin did not confirm it can deactivate") from exc
            if ready is not True:
                raise HTTPException(409, "The printer plugin reports an active operation")
    if any(printer_id in ids and not task.done() for task, printer_id in print_scheduler._inflight.values()):
        raise HTTPException(409, "An affected printer has an in-flight upload")
    if any(print_scheduler._printer_in_dispatch_hold(printer_id) for printer_id in ids):
        raise HTTPException(409, "An affected printer has an active dispatch")
    if ids and await db.scalar(
        select(func.count())
        .select_from(PrintQueueItem)
        .where(
            PrintQueueItem.printer_id.in_(ids),
            PrintQueueItem.status.in_(("printing", "uploading", "starting", "paused")),
        )
    ):
        raise HTTPException(409, "An affected printer has an active queue job")
    if ids and await db.scalar(
        select(func.count())
        .select_from(PrinterOperation)
        .where(
            PrinterOperation.printer_id.in_(ids),
            PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")),
        )
    ):
        raise HTTPException(409, "An affected printer has an active SDK operation")


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
    registry = _registry()
    await _require_idle(db, plugin_id)
    await _validate_saved_configuration(db, plugin_id, body.version)
    source = registry.loaded_sources.get(plugin_id, {})
    loaded_version = "bundled" if source.get("source") == "bundled" else source.get("version")
    return await asyncio.to_thread(PluginManager().select, plugin_id, body.version, loaded_version=loaded_version)


@router.post("/{plugin_id}/rollback")
async def rollback_printer_package(
    plugin_id: str,
    body: PluginRollbackSelection | None = None,
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    registry = _registry()
    await _require_idle(db, plugin_id)
    target = PluginManager().rollback_target(plugin_id, body.version if body else None)
    await _validate_saved_configuration(db, plugin_id, target)
    source = registry.loaded_sources.get(plugin_id, {})
    loaded_version = "bundled" if source.get("source") == "bundled" else source.get("version")
    return await asyncio.to_thread(
        PluginManager().rollback,
        plugin_id,
        version=target,
        loaded_version=loaded_version,
    )


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
    registry = _registry()
    await _require_idle(db, plugin_id, require_unconfigured=True)
    return await asyncio.to_thread(PluginManager().set_enabled, plugin_id, False, loaded=plugin_id in registry._plugins)


@router.delete("/{plugin_id}/versions/{version}")
async def remove_printer_package_version(plugin_id: str, version: str, _=Depends(_plugin_administrator)):
    registry = _registry()
    loaded_version = registry.loaded_sources.get(plugin_id, {}).get("version")
    return await asyncio.to_thread(PluginManager().remove_version, plugin_id, version, loaded_version=loaded_version)


@router.delete("/{plugin_id}")
async def remove_printer_plugin(
    plugin_id: str,
    _=Depends(_plugin_administrator),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    _registry()
    await _require_idle(db, plugin_id, require_unconfigured=True)
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

"""Keep one plugin-global service owner while printer versions drain."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from backend.app.printers.contracts import UnsupportedPrinterOperation
from backend.app.printers.registry import RuntimeProvider, current_registry, provider_service_context

logger = logging.getLogger(__name__)


class PluginLifecycle:
    def __init__(self):
        self._owners: dict[str, RuntimeProvider] = {}
        self._virtual_services: dict[int, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._host: Any = None
        self.errors: dict[str, str] = {}

    @asynccontextmanager
    async def change_lock(self, plugin_id: str):
        async with self._locks.setdefault(plugin_id, asyncio.Lock()):
            yield

    def _application(self):
        if self._host is None:
            from backend.app import main

            self._host = main
        return self._host

    async def _start(self, candidate: RuntimeProvider) -> None:
        host = self._application()
        plugin = candidate.plugin
        with provider_service_context(plugin):
            try:
                await plugin.start_host_services(host)
                try:
                    service = plugin.virtual_printer_service()
                except UnsupportedPrinterOperation:
                    return
                service.set_session_factory(host.async_session)
                service.set_printer_manager(host.printer_manager)
                await service.sync_from_db()
                self._virtual_services[id(plugin)] = service
            except BaseException:
                await plugin.stop_host_services(host)
                raise

    async def _stop(self, candidate: RuntimeProvider) -> None:
        with provider_service_context(candidate.plugin):
            service = self._virtual_services.pop(id(candidate.plugin), None)
            if service is not None:
                await service.stop_all()
            await candidate.plugin.stop_host_services(self._application())

    async def prepare(self, candidate: RuntimeProvider) -> bool:
        """Compile routes; first provider services start before publishing it."""
        from backend.app.printers import http_gateway

        if http_gateway.gateway is not None:
            http_gateway.gateway.prepare(candidate.plugin)
        if candidate.plugin.id in self._owners:
            return False
        await self._start(candidate)
        self._owners[candidate.plugin.id] = candidate
        self.errors.pop(candidate.plugin.id, None)
        return True

    async def discard(self, candidate: RuntimeProvider) -> None:
        if self._owners.get(candidate.plugin.id) is candidate:
            await self._stop(candidate)
            self._owners.pop(candidate.plugin.id, None)

    async def start(self, host) -> None:
        self._host = host
        registry = current_registry()
        await self._restore_pending_operations()
        for plugin in registry.all():
            try:
                await self.prepare(registry.runtime_for(plugin))
            except Exception as exc:
                self.errors[plugin.id] = f"{type(exc).__name__}: {exc}"
                logger.exception("Failed to start printer plugin host services for %s", plugin.id)

    async def _restore_pending_operations(self) -> None:
        from sqlalchemy import select

        from backend.app.models.printer import Printer
        from backend.app.models.printer_operation import PrinterOperation
        from backend.app.printers.contracts import PrinterConfig
        from backend.app.printers.management import printer_pin_identity

        registry = current_registry()
        async with self._application().async_session() as db:
            pending = (
                await db.execute(
                    select(PrinterOperation, Printer)
                    .join(Printer, Printer.id == PrinterOperation.printer_id)
                    .where(PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")))
                )
            ).all()
        for operation, printer in pending:
            binding = operation.operation_schema.get("extensions", {}).get("bambuddy:provider")
            config = PrinterConfig.from_printer(printer)
            if not (
                isinstance(binding, dict)
                and binding.get("plugin_id") == config.plugin_id
                and binding.get("identity") == printer_pin_identity(config)
                and isinstance(binding.get("version"), str)
            ):
                continue
            try:
                candidate = await asyncio.to_thread(registry.prepare, config.plugin_id, binding["version"])
                registry.retain_runtime(
                    candidate.plugin,
                    f"operation:{operation.id}",
                    printer_id=printer.id,
                    reason="pending operation",
                )
            except Exception:
                logger.exception("Unable to restore provider for pending printer operation %s", operation.id)

    def protected_versions(self, plugin_id: str) -> set[str]:
        owner = self._owners.get(plugin_id)
        return {owner.version} if owner is not None else set()

    async def reconcile(self, plugin_id: str | None = None) -> None:
        registry = current_registry()
        ids = [plugin_id] if plugin_id is not None else [plugin.id for plugin in registry.all()]
        for provider_id in ids:
            async with self.change_lock(provider_id):
                plugin = registry.get(provider_id)
                desired = registry.runtime_for(plugin)
                owner = self._owners.get(provider_id)
                if owner is desired:
                    continue
                if owner is not None:
                    from backend.app.services.printer_manager import printer_manager

                    if owner.version in registry.protected_runtime_versions(provider_id) or any(
                        provider is owner.plugin for provider in printer_manager.retained_providers()
                    ):
                        continue
                if owner is not None:
                    await self._stop(owner)
                try:
                    await self._start(desired)
                except Exception as exc:
                    self.errors[provider_id] = f"{type(exc).__name__}: {exc}"
                    if owner is not None:
                        try:
                            await self._start(owner)
                        except Exception:
                            logger.exception("Failed to restore old printer plugin host services for %s", provider_id)
                    raise
                self._owners[provider_id] = desired
                self.errors.pop(provider_id, None)

    async def stop(self) -> None:
        """Call only after the printer manager has disconnected every observer."""
        registry = current_registry()
        retained = {id(candidate.plugin): candidate for candidate in self._owners.values()}
        for plugin in registry.runtime_plugins():
            retained.setdefault(id(plugin), registry.runtime_for(plugin))
        for candidate in reversed(tuple(retained.values())):
            try:
                await self._stop(candidate)
            except Exception:
                logger.exception("Failed to stop printer plugin host services for %s", candidate.plugin.id)
        self._owners.clear()
        self._virtual_services.clear()


lifecycle = PluginLifecycle()

"""Route plugin HTTP extensions through the device's retained provider version."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException
from fastapi.openapi.utils import get_openapi
from starlette.routing import BaseRoute, Match, NoMatchFound

from backend.app.core.config import settings
from backend.app.printers.contracts import PrinterConfig, PrinterPluginError


class PluginHTTPGateway(BaseRoute):
    def __init__(self, app):
        self.app = app
        self._routers: dict[int, tuple[Any, APIRouter]] = {}
        self._openapi_key: tuple | None = None

    def prepare(self, plugin) -> APIRouter:
        cached = self._routers.get(id(plugin))
        if cached is not None:
            return cached[1]
        else:
            router = APIRouter(prefix=settings.api_prefix, dependency_overrides_provider=self.app)
            for extension in plugin.legacy_http_routers():
                router.include_router(extension)
            self._routers[id(plugin)] = (plugin, router)
            self.app.openapi_schema = None
        return router

    def openapi(self) -> dict:
        """Document the selected defaults without duplicating live routes."""
        from backend.app.printers.registry import current_registry

        defaults = current_registry().all()
        key = (tuple(id(plugin) for plugin in defaults), tuple(id(route) for route in self.app.routes))
        if self.app.openapi_schema is None or self._openapi_key != key:
            extensions = [route for plugin in defaults for route in self.prepare(plugin).routes]
            self.app.openapi_schema = get_openapi(
                title=self.app.title,
                version=self.app.version,
                openapi_version=self.app.openapi_version,
                summary=self.app.summary,
                description=self.app.description,
                terms_of_service=self.app.terms_of_service,
                contact=self.app.contact,
                license_info=self.app.license_info,
                routes=[*self.app.routes, *extensions],
                webhooks=self.app.webhooks.routes,
                tags=self.app.openapi_tags,
                servers=self.app.servers,
                separate_input_output_schemas=self.app.separate_input_output_schemas,
                external_docs=self.app.openapi_external_docs,
            )
            self._openapi_key = key
        return self.app.openapi_schema

    def _matches(self, plugin, scope):
        partial = None
        for route in self.prepare(plugin).routes:
            match, child = route.matches(scope)
            if match == Match.FULL:
                return route, match, child
            if match == Match.PARTIAL and partial is None:
                partial = (route, match, child)
        return partial

    def matches(self, scope):
        if scope["type"] != "http":
            return Match.NONE, {}
        from backend.app.printers.registry import current_registry

        partial = None
        for plugin in current_registry().runtime_plugins():
            found = self._matches(plugin, scope)
            if found is not None:
                if found[1] == Match.FULL:
                    return Match.FULL, {}
                partial = found
        return (Match.PARTIAL, {}) if partial is not None else (Match.NONE, {})

    async def handle(self, scope, receive, send):
        from sqlalchemy import select

        from backend.app.core import database
        from backend.app.models.printer import Printer
        from backend.app.printers.api import config_for_id
        from backend.app.printers.registry import current_registry

        registry = current_registry()
        plugins = registry.runtime_plugins()
        matches = [(plugin, self._matches(plugin, scope)) for plugin in plugins]
        matches = [(plugin, match) for plugin, match in matches if match is not None]
        printer_ids = {
            match[2].get("path_params", {}).get("printer_id")
            for _, match in matches
            if match[2].get("path_params", {}).get("printer_id") is not None
        }
        if not printer_ids:
            try:
                query_ids = parse_qs(scope.get("query_string", b"").decode("ascii"), max_num_fields=100).get(
                    "printer_id", []
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(400, "Invalid printer query parameters") from exc
            printer_ids.update(query_ids)
        if len(printer_ids) > 1:
            raise HTTPException(400, "Conflicting printer identities")
        if printer_ids:
            value = next(iter(printer_ids))
            try:
                printer_id = int(value)
                if not 0 < printer_id < 2**63:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "Invalid printer identity") from exc
            config = config_for_id(printer_id)
            if config is None:
                async with database.async_session() as db:
                    printer = await db.scalar(select(Printer).where(Printer.id == printer_id))
                    config = PrinterConfig.from_printer(printer) if printer is not None else None
            if config is None:
                raise HTTPException(404, "Printer not found")
            try:
                plugin = registry.provider_for(config)
            except PrinterPluginError as exc:
                raise HTTPException(
                    409, "This printer's provider is unavailable; use its generic SDK controls after enabling it"
                ) from exc
            found = self._matches(plugin, scope)
            if found is None:
                raise HTTPException(409, "This printer plugin requires its generic SDK controls for that operation")
        else:
            # Plugin-global account/catalog routes follow the selected default.
            found = next(
                (self._matches(plugin, scope) for plugin in registry.all() if self._matches(plugin, scope)), None
            )
            if found is None:
                raise HTTPException(404, "Printer plugin endpoint not found")
        route, _, child = found
        await route.handle({**scope, **child}, receive, send)

    def url_path_for(self, name, **path_params):
        from backend.app.printers.registry import current_registry

        for plugin in current_registry().all():
            try:
                return self.prepare(plugin).url_path_for(name, **path_params)
            except NoMatchFound:
                continue
        raise NoMatchFound(name, path_params)


gateway: PluginHTTPGateway | None = None


def install_plugin_gateway(app) -> PluginHTTPGateway:
    global gateway
    gateway = PluginHTTPGateway(app)
    app.router.routes.append(gateway)
    app.openapi = gateway.openapi
    return gateway

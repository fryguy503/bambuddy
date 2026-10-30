"""HTTP extensions follow pinned instances and can be added without restart."""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.http_gateway import install_plugin_gateway


@pytest.mark.asyncio
async def test_gateway_hot_adds_routes_and_uses_old_pinned_version_with_auth_overrides(monkeypatch):
    from backend.app.printers import api, http_gateway, registry as registry_module

    async def authorization():
        raise AssertionError("The application dependency override must be preserved")

    def plugin(version, *, extra=False):
        router = APIRouter()

        @router.get("/printers/{printer_id}/plugin-version")
        async def printer_version(printer_id: int, allowed=Depends(authorization)):
            return {"version": version, "printer_id": printer_id, "allowed": allowed}

        @router.get("/plugin-global")
        async def global_version():
            return {"version": version}

        if extra:

            @router.get("/new-plugin-route")
            async def newly_added():
                return {"version": version}

        return SimpleNamespace(id="example", version=version, legacy_http_routers=lambda: (router,))

    old, new = plugin("1.0.0"), plugin("2.0.0", extra=True)
    runtime = [old]
    default = [old]
    registry = SimpleNamespace(
        runtime_plugins=lambda: tuple(runtime),
        all=lambda: tuple(default),
        provider_for=lambda config: old if config.printer_id == 1 else default[0],
    )
    monkeypatch.setattr(registry_module, "current_registry", lambda: registry)
    monkeypatch.setattr(http_gateway, "gateway", None)
    monkeypatch.setattr(
        api,
        "config_for_id",
        lambda printer_id: PrinterConfig(
            "",
            f"device-{printer_id}",
            "",
            plugin_id="example",
            printer_id=printer_id,
        ),
    )
    app = FastAPI()
    app.dependency_overrides[authorization] = lambda: "authorized"
    install_plugin_gateway(app)
    initial_routes = len(app.routes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/new-plugin-route")).status_code == 404
        assert (await client.get("/api/v1/plugin-global")).json() == {"version": "1.0.0"}
        runtime.append(new)
        default[:] = [new]
        pinned = await client.get("/api/v1/printers/1/plugin-version")
        current = await client.get("/api/v1/printers/2/plugin-version")
        assert pinned.json() == {"version": "1.0.0", "printer_id": 1, "allowed": "authorized"}
        assert current.json() == {"version": "2.0.0", "printer_id": 2, "allowed": "authorized"}
        assert (await client.get("/api/v1/plugin-global")).json() == {"version": "2.0.0"}
        assert (await client.get("/api/v1/new-plugin-route")).json() == {"version": "2.0.0"}
        assert (await client.get("/api/v1/plugin-global?printer_id=1")).json() == {"version": "1.0.0"}
    assert len(app.routes) == initial_routes


def test_openapi_lists_selected_default_extensions_and_refreshes_without_duplicate_live_routes(monkeypatch):
    from backend.app.printers import http_gateway, registry as registry_module

    def plugin(version, path):
        router = APIRouter()

        @router.get("/shared-plugin-route", summary=f"Provider {version}")
        async def shared():
            return {"version": version}

        @router.get(path)
        async def version_only():
            return {"version": version}

        return SimpleNamespace(id="example", version=version, legacy_http_routers=lambda: (router,))

    old, new = plugin("1.0.0", "/old-version-only"), plugin("2.0.0", "/new-version-only")
    selected = [old]
    monkeypatch.setattr(registry_module, "current_registry", lambda: SimpleNamespace(all=lambda: tuple(selected)))
    monkeypatch.setattr(http_gateway, "gateway", None)
    app = FastAPI(title="Printer API", version="9.8.7", summary="Application API", description="Core description")

    @app.get("/core", tags=["core"])
    async def core():
        return {"core": True}

    gateway = install_plugin_gateway(app)
    live_routes = tuple(app.routes)
    before = app.openapi()
    assert app.openapi() is before
    assert before["info"] == {
        "title": "Printer API",
        "version": "9.8.7",
        "summary": "Application API",
        "description": "Core description",
    }
    assert "/core" in before["paths"]
    assert before["paths"]["/api/v1/shared-plugin-route"]["get"]["summary"] == "Provider 1.0.0"
    assert "/api/v1/old-version-only" in before["paths"]

    gateway.prepare(new)
    assert app.openapi_schema is None
    staged = app.openapi()
    assert "/api/v1/new-version-only" not in staged["paths"]
    selected[:] = [new]
    after = app.openapi()
    assert after is not staged
    assert after["paths"]["/api/v1/shared-plugin-route"]["get"]["summary"] == "Provider 2.0.0"
    assert "/api/v1/new-version-only" in after["paths"]
    assert "/api/v1/old-version-only" not in after["paths"]
    assert tuple(app.routes) == live_routes

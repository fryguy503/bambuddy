"""Global workers follow one owner until old operation references finish."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.app.printers.contracts import UnsupportedPrinterOperation
from backend.app.printers.lifecycle import PluginLifecycle
from backend.app.printers.registry import RuntimeProvider


def candidate(version):
    plugin = SimpleNamespace(
        id="example",
        version=version,
        start_host_services=AsyncMock(),
        stop_host_services=AsyncMock(),
        virtual_printer_service=Mock(side_effect=UnsupportedPrinterOperation("none")),
        legacy_http_routers=lambda: (),
    )
    return RuntimeProvider(
        plugin,
        version,
        {
            "source": "managed-package",
            "version": version,
            "module_namespace": "test_plugins.v" + version.replace(".", "_"),
        },
    )


@pytest.mark.asyncio
async def test_global_worker_handover_waits_for_old_operation_refs_not_current_observer(monkeypatch):
    from backend.app.printers import http_gateway, lifecycle as lifecycle_module
    from backend.app.services.printer_manager import printer_manager

    old, new = candidate("1.0.0"), candidate("2.0.0")
    refs = {old.version}
    registry = SimpleNamespace(
        get=lambda _: new.plugin,
        runtime_for=lambda plugin: old if plugin is old.plugin else new,
        protected_runtime_versions=lambda _: set(refs),
    )
    monkeypatch.setattr(lifecycle_module, "current_registry", lambda: registry)
    monkeypatch.setattr(http_gateway, "gateway", None)
    monkeypatch.setattr(printer_manager, "retained_providers", lambda: (), raising=False)
    lifecycle = PluginLifecycle()
    lifecycle._host = SimpleNamespace()
    assert await lifecycle.prepare(old)
    assert not await lifecycle.prepare(new)
    await lifecycle.reconcile("example")
    old.plugin.stop_host_services.assert_not_called()
    new.plugin.start_host_services.assert_not_called()
    assert lifecycle.protected_versions("example") == {old.version}
    refs.clear()
    await lifecycle.reconcile("example")
    old.plugin.stop_host_services.assert_awaited_once()
    new.plugin.start_host_services.assert_awaited_once()
    assert lifecycle.protected_versions("example") == {new.version}


@pytest.mark.asyncio
async def test_failed_new_global_worker_restores_old_owner(monkeypatch):
    from backend.app.printers import compat, http_gateway, lifecycle as lifecycle_module, registry as registry_module
    from backend.app.services.printer_manager import printer_manager

    old, new = candidate("1.0.0"), candidate("2.0.0")
    registry = SimpleNamespace(
        load=lambda: None,
        loaded_sources={"example": new.source},
        get=lambda _: new.plugin,
        runtime_for=lambda plugin: old if plugin is old.plugin else new,
        protected_runtime_versions=lambda _: set(),
    )
    monkeypatch.setattr(lifecycle_module, "current_registry", lambda: registry)
    monkeypatch.setattr(registry_module, "current_registry", lambda: registry)
    monkeypatch.setattr(compat, "import_module", lambda name: name)
    monkeypatch.setattr(http_gateway, "gateway", None)
    monkeypatch.setattr(printer_manager, "retained_providers", lambda: (), raising=False)
    lifecycle = PluginLifecycle()
    lifecycle._host = SimpleNamespace()
    sources = []

    async def start_old(_host):
        sources.append("start:" + compat.compatibility_module("example", "history"))

    async def stop_old(_host):
        sources.append("stop:" + compat.compatibility_module("example", "history"))

    async def start_new(_host):
        sources.append("start:" + compat.compatibility_module("example", "history"))
        raise RuntimeError("worker failed")

    async def stop_new(_host):
        sources.append("stop:" + compat.compatibility_module("example", "history"))

    old.plugin.start_host_services.side_effect = start_old
    old.plugin.stop_host_services.side_effect = stop_old
    new.plugin.start_host_services.side_effect = start_new
    new.plugin.stop_host_services.side_effect = stop_new
    await lifecycle.prepare(old)
    with pytest.raises(RuntimeError, match="worker failed"):
        await lifecycle.reconcile("example")
    assert old.plugin.start_host_services.await_count == 2
    assert lifecycle.protected_versions("example") == {old.version}
    assert "RuntimeError" in lifecycle.errors["example"]
    assert sources == [
        "start:test_plugins.v1_0_0.history",
        "stop:test_plugins.v1_0_0.history",
        "start:test_plugins.v2_0_0.history",
        "stop:test_plugins.v2_0_0.history",
        "start:test_plugins.v1_0_0.history",
    ]

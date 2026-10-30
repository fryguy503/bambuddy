"""Retained workflows and global workers resolve their own package helpers."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.registry import RuntimeProvider, provider_context, provider_service_context


@pytest.mark.asyncio
async def test_old_workflow_resolves_old_helpers_after_default_upgrade_and_across_task_boundary(monkeypatch):
    from backend.app.printers import compat, registry as registry_module

    old = SimpleNamespace(id="example", version="1.0.0")
    new = SimpleNamespace(id="example", version="2.0.0")
    runtimes = {
        id(old): RuntimeProvider(old, "1.0.0", {"module_namespace": "fixture.old"}),
        id(new): RuntimeProvider(new, "2.0.0", {"module_namespace": "fixture.new"}),
    }
    registry = SimpleNamespace(
        load=lambda: None,
        runtime_for=lambda provider: runtimes[id(provider)],
        loaded_sources={"example": runtimes[id(new)].source, "other": {"module_namespace": "fixture.other"}},
    )
    monkeypatch.setattr(registry_module, "current_registry", lambda: registry)
    monkeypatch.setattr(compat, "import_module", lambda name: name)
    config = PrinterConfig("", "device", "", plugin_id="example", printer_id=1)

    async def callback():
        await asyncio.sleep(0)
        return compat.compatibility_module("example", "workflows")

    assert compat.compatibility_module("example", "workflows") == "fixture.new.workflows"
    with provider_context(config, old):
        task = asyncio.create_task(callback())
        assert compat.compatibility_module("other", "models") == "fixture.other.models"
    assert await task == "fixture.old.workflows"
    assert compat.compatibility_module("example", "workflows") == "fixture.new.workflows"
    with provider_service_context(old):
        assert compat.compatibility_module("example", "history") == "fixture.old.history"
    assert compat.compatibility_module("example", "history") == "fixture.new.history"

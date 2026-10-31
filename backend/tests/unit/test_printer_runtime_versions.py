"""A refreshed observer must not steal an older driver's accepted work."""

import asyncio
from types import MethodType, SimpleNamespace

import pytest

from backend.app.core.config import settings
from backend.app.printers import api
from backend.app.printers.contracts import PrinterConfig, PrinterModel, PrinterPlugin, PrinterPluginError
from backend.app.printers.management import PluginManager
from backend.app.printers.operation_versions import (
    bind_operation_schema,
    operation_connection,
    operation_provider,
    release_terminal_operation,
)
from backend.app.printers.registry import PrinterPluginRegistry, RuntimeProvider, provider_context
from backend.app.printers.sdk.host import HostPrinterState, state_for_host
from backend.app.printers.state_accessors import loaded_materials
from backend.tests.unit.test_printer_plugin_packages import package_bytes


class VersionedProvider(PrinterPlugin):
    id = "versioned"
    name = "Versioned driver"
    models = (PrinterModel("fixture", "Fixture"),)
    operations = ("pause_print",)

    def __init__(self, version):
        self.version = version
        self.calls = []

    def create_connection(self, config, **callbacks):
        return SimpleNamespace(state=HostPrinterState(connected=True, state="RUNNING"), callbacks=callbacks)

    def command_handlers(self, connection):
        return {"pause_print": lambda: self.calls.append("pause") or self.version}

    def loaded_materials(self, state):
        return [{"version": self.version}]


@pytest.fixture
def versions(tmp_path, monkeypatch):
    from backend.app.printers import registry as registry_module

    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "plugins")
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    registry = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(api, "_bindings", {})
    registry.load()
    old, new = VersionedProvider("1.0.0"), VersionedProvider("1.1.0")
    registry.register(old)
    candidate = RuntimeProvider(new, new.version, {"version": new.version, "source": "managed-package"})
    config = PrinterConfig("192.168.50.50", "VERSIONED-ONE", "", "fixture", old.id, 42)
    registry.pin_printer(config, old)
    api.bind_printer(config)
    registry.activate(candidate)
    return registry, old, new, config


def test_default_refresh_does_not_reroute_a_pinned_running_printer(versions):
    registry, old, new, config = versions
    assert registry.get(old.id) is new
    assert registry.provider_for(config) is old
    old_connection = api.printer_api.create_connection(config)
    assert old_connection.state.state == "RUNNING"
    assert old_connection.pause_print() == old.version
    assert old.calls == ["pause"] and new.calls == []
    assert {item["version"] for item in registry.running_versions(old.id)} == {old.version, new.version}


def test_bundled_catalog_uses_its_static_model_presentation_for_print_options(versions):
    registry, _, _, _ = versions
    bundled = registry.get("bambu")
    candidate = registry.runtime_for(bundled)
    assert bundled._installed_manifest is candidate.manifest
    models = bundled.descriptor()["models"]
    assert models and all(model["presentation"]["compatibility_ui"] == "bambu-v1" for model in models)


def test_explicit_candidate_connection_does_not_publish_it_before_commit(versions):
    registry, old, new, config = versions
    candidate = api.printer_api.create_connection(config, provider=new)
    assert candidate._plugin is new
    assert registry.provider_for(config) is old
    registry.pin_printer(config, new)
    assert registry.provider_for(config) is new
    assert candidate.pause_print() == new.version


async def test_old_workflow_context_survives_async_scheduling_without_leaking(versions):
    registry, old, new, config = versions
    original = api.printer_api.create_connection(config)
    registry.pin_printer(config, new)

    async def finish_original_work():
        await asyncio.sleep(0)
        assert registry.provider_for(config) is old
        return original.pause_print()

    with provider_context(config, old):
        pending = asyncio.create_task(finish_original_work())
    assert registry.provider_for(config) is new
    assert await pending == old.version
    assert registry.provider_for(config) is new
    with pytest.raises(PrinterPluginError, match="changed"):
        original.pause_print()


async def test_awaited_io_retains_its_provider_after_observer_refresh(versions):
    registry, old, new, config = versions
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_call(self, connection_config, operation):
        entered.set()
        await release.wait()
        return "original response"

    old.file_operation_for = MethodType(slow_call, old)
    with provider_context(config, old):
        value = api.leased_call(config, old.loaded_materials, HostPrinterState())
        assert value == [{"version": old.version}]
        call = api.printer_api.files(config, "inspect")
        task = asyncio.create_task(call)
    await entered.wait()
    registry.pin_printer(config, new)
    assert old.version in registry.protected_runtime_versions(old.id)
    # The caller may continue on the already captured implementation. No
    # printing-state condition or new queued-job drain is required.
    release.set()
    assert await task == "original response"
    assert old.version not in registry.protected_runtime_versions(old.id)
    assert registry.provider_for(config) is new


async def test_legacy_file_dispatch_retains_provider_across_live_refresh(versions):
    from backend.app.printers.files import download_file_async

    registry, old, new, config = versions
    entered, release = asyncio.Event(), asyncio.Event()

    async def download(self, connection_config, operation, *args, **kwargs):
        entered.set()
        await release.wait()
        assert registry.provider_for(connection_config) is old
        return b"original download"

    old.file_operation_for = MethodType(download, old)
    with provider_context(config, old):
        task = asyncio.create_task(download_file_async(config.ip_address, printer_id=config.printer_id))
    await entered.wait()
    registry.pin_printer(config, new)
    assert old.version in registry.protected_runtime_versions(old.id)
    release.set()
    assert await task == b"original download"
    assert old.version not in registry.protected_runtime_versions(old.id)
    assert registry.provider_for(config) is new


async def test_internal_sdk_invocation_keeps_nested_helpers_on_original_provider(versions):
    from backend.app.printers.sdk import DeviceDescriptor, OperationDescriptor, OperationReceipt, OperationRequest

    registry, old, new, config = versions
    entered, release = asyncio.Event(), asyncio.Event()
    descriptor = DeviceDescriptor(
        device_id="fixture:device",
        operations=[OperationDescriptor(id="test.read", label="Read fixture", result_schema={"type": "string"})],
    )
    old.describe_device = MethodType(lambda self, config: descriptor, old)
    old.file_operation_for = MethodType(lambda self, config, operation: self.version, old)

    async def invoke(self, connection_config, raw, request):
        entered.set()
        await release.wait()
        assert registry.provider_for(connection_config) is old
        result = api.printer_api.files(connection_config, "inspect")
        return OperationReceipt(
            operation_id=request.operation_id, request_id=request.request_id, status="succeeded", result=result
        )

    old.invoke = MethodType(invoke, old)
    connection = api.printer_api.create_connection(config)
    pending = asyncio.create_task(
        api.printer_api.invoke(config, connection, OperationRequest(operation_id="test.read"))
    )
    await entered.wait()
    registry.pin_printer(config, new)
    release.set()
    assert (await pending).result == old.version
    assert registry.provider_for(config) is new


def test_state_normalization_keeps_its_originating_provider(versions):
    registry, old, new, config = versions
    state = state_for_host(config, old, HostPrinterState(connected=True, state="RUNNING"))
    registry.pin_printer(config, new)
    assert loaded_materials(state, plugin_id=old.id) == [{"version": old.version}]


def test_receipt_polling_uses_retained_original_connection_after_refresh(versions, monkeypatch):
    from backend.app.services.printer_manager import printer_manager

    registry, old, new, config = versions
    original = api.printer_api.create_connection(config)
    schema = bind_operation_schema({"extensions": {}}, config, old)
    row = SimpleNamespace(id="receipt-1", operation_schema=schema, receipt={"status": "accepted"})
    registry.pin_printer(config, new)
    current = api.printer_api.create_connection(config)
    monkeypatch.setattr(printer_manager, "get_client", lambda _id: current)
    monkeypatch.setattr(
        printer_manager, "retained_connection", lambda _id, provider: original if provider is old else None
    )
    assert operation_provider(row, config) is old
    assert operation_connection(config.printer_id, old) is original._connection
    assert old.version in registry.protected_runtime_versions(old.id)
    row.receipt = {"status": "succeeded"}
    release_terminal_operation(row)
    assert old.version not in registry.protected_runtime_versions(old.id)
    assert old.calls == [] and new.calls == []


def test_corrupt_receipt_binding_cannot_select_another_printers_provider(versions):
    _, old, _, config = versions
    schema = bind_operation_schema({"extensions": {}}, config, old)
    schema["extensions"]["bambuddy:provider"]["identity"] = "0" * 64
    row = SimpleNamespace(id="receipt-1", operation_schema=schema, receipt={"status": "running"})
    with pytest.raises(PrinterPluginError, match="identity"):
        operation_provider(row, config)


def test_durable_version_pin_survives_process_restart(versions, monkeypatch):
    from backend.app.printers import registry as registry_module

    manager = PluginManager()
    manager.stage(package_bytes(version="1.0.0"))
    manager.stage(package_bytes(version="1.1.0"))
    manager.select("example", "1.1.0")
    config = PrinterConfig("192.168.50.51", "EXAMPLE-ONE", "", plugin_id="example", printer_id=7)
    manager.set_printer_pin(config, "1.0.0")
    fresh = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", fresh)
    assert fresh.get("example").version == "1.1.0"
    assert fresh.provider_for(config).version == "1.0.0"
    assert fresh.prepare("example", "1.0.0").plugin is fresh.provider_for(config)

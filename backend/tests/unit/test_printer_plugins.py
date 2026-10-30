"""Plugin extension regression tests, using a non-vendor test adapter."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.printers import api, files
from backend.app.printers.bambu.plugin import BambuPrinterPlugin
from backend.app.printers.contracts import (
    PrinterConfig,
    PrinterModel,
    PrinterPlugin,
    PrinterPluginError,
    UnknownPrinterPlugin,
    UnsupportedPrinterOperation,
)
from backend.app.printers.registry import PrinterPluginRegistry
from backend.app.printers.state import PrinterState


class ExamplePrinterPlugin(PrinterPlugin):
    id = "example"
    name = "Example printer addon"
    models = (PrinterModel("sample", "Sample", capabilities=frozenset({"print", "files"})),)
    operations = ("pause",)

    def create_connection(self, config, **callbacks):
        return SimpleNamespace(state=PrinterState(), callbacks=callbacks)

    def command_handlers(self, connection):
        return {"pause": connection.pause}

    async def file_operation(self, operation, *args, **kwargs):
        if operation != "list_files_async":
            raise UnsupportedPrinterOperation(operation)
        return [{"name": "sample.gcode"}]


@pytest.fixture
def isolated_registry(monkeypatch):
    from backend.app.printers import registry as registry_module

    registry = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(api, "_bindings", {})
    return registry


def config(plugin_id="example", printer_id=21, ip="192.0.2.21"):
    return PrinterConfig(ip, "CaseSensitive", "secret", "sample", plugin_id, printer_id)


def test_bundled_catalog_is_bambu_only_with_model_capabilities(isolated_registry):
    descriptor = isolated_registry.descriptors()[0]
    assert descriptor["id"] == "bambu"
    assert descriptor["bundled"] is True
    assert len(descriptor["models"]) == 14
    plugin = isolated_registry.get("bambu")
    assert plugin.model("O1E").id == "H2D Pro"
    assert plugin.model("Bambu Lab X1 Carbon").id == "X1C"
    assert "chamber_heater" not in plugin.model_capabilities("X1C")
    assert "chamber_heater" in plugin.model_capabilities("H2D Pro")
    assert "bed_slinger" in plugin.model_capabilities("A1 Mini")
    assert "aux_fan_left" in plugin.model_capabilities("X2D")
    assert "aux_fan_left" not in plugin.model_capabilities("P2S")


def test_installed_entry_point_registers_without_core_changes(monkeypatch):
    from backend.app.printers import registry as registry_module

    entry = SimpleNamespace(name="example", load=lambda: ExamplePrinterPlugin)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [entry])
    registry = PrinterPluginRegistry()
    assert registry.get("example").model("sample").name == "Sample"
    assert [plugin.id for plugin in registry.all()] == ["bambu", "example"]


def test_broken_or_duplicate_addon_does_not_replace_bambu(monkeypatch):
    from backend.app.printers import registry as registry_module

    duplicate = SimpleNamespace(name="imposter", load=lambda: BambuPrinterPlugin)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [duplicate])
    registry = PrinterPluginRegistry()
    assert isinstance(registry.get("bambu"), BambuPrinterPlugin)
    assert "imposter" in registry.load_errors


def test_registry_rejects_unknown_and_incompatible_plugins(isolated_registry):
    with pytest.raises(UnknownPrinterPlugin):
        isolated_registry.get("missing")
    plugin = ExamplePrinterPlugin()
    plugin.api_version = 99
    with pytest.raises(PrinterPluginError, match="API version"):
        isolated_registry.register(plugin)


def test_plugin_serial_and_secrets_contract(isolated_registry):
    plugin = ExamplePrinterPlugin()
    assert plugin.normalize_serial_number(" MiXeD ") == "MiXeD"
    assert isolated_registry.get("bambu").normalize_serial_number(" MiXeD ") == "MIXED"
    assert "secret" not in repr(config())
    assert "options" not in repr(PrinterConfig("ip", "serial", "key", options={"token": "secret"}))


def test_addon_command_is_allowlisted_and_validated_before_call():
    plugin = ExamplePrinterPlugin()
    connection = SimpleNamespace(pause=Mock(spec=lambda speed=1: True))
    # Use a real callable so inspect.signature sees the adapter's argument contract.
    calls = []

    def pause(speed=1):
        calls.append(speed)
        return True

    connection.pause = pause
    assert plugin.execute(config(), connection, "pause", {"speed": 2}) is True
    with pytest.raises(TypeError):
        plugin.execute(config(), connection, "pause", {"unknown": 3})
    with pytest.raises(UnsupportedPrinterOperation):
        plugin.execute(config(), connection, "__dict__", {})
    assert calls == [2]


@pytest.mark.asyncio
async def test_file_dispatch_uses_registered_addon_and_unknown_fails_closed(isolated_registry):
    isolated_registry.register(ExamplePrinterPlugin())
    files.bind_printer(config())
    assert await files.list_files_async("192.0.2.21", "secret") == [{"name": "sample.gcode"}]
    with pytest.raises(UnknownPrinterPlugin):
        await files.list_files_async("192.0.2.21", "secret", plugin_id="missing")
    with pytest.raises(UnsupportedPrinterOperation):
        await files.upload_file_async("192.0.2.21", "secret", "local", "remote")
    files.unbind_printer(21)
    assert api.config_for_ip("192.0.2.21") is None


def test_conflicting_ip_requires_explicit_plugin(isolated_registry):
    isolated_registry.register(ExamplePrinterPlugin())
    api.bind_printer(config())
    api.bind_printer(config("bambu", 22))
    with pytest.raises(PrinterPluginError, match="Multiple printer plugins"):
        api.plugin_for_ip("192.0.2.21")
    assert api.plugin_for_ip("192.0.2.21", plugin_id="example").id == "example"


def test_bambu_state_and_file_results_keep_legacy_identity():
    from backend.app.printers import file_types
    from backend.app.printers.bambu import ftp, mqtt
    from backend.app.services import bambu_ftp, bambu_mqtt

    assert mqtt.PrinterState is PrinterState
    assert bambu_mqtt.PrinterState is PrinterState
    assert mqtt.NozzleInfo is bambu_mqtt.NozzleInfo
    assert ftp.FileListResult is file_types.FileListResult
    assert bambu_ftp.DeleteResult is file_types.DeleteResult


def test_bambu_factory_forwards_callbacks_and_model(monkeypatch):
    from backend.app.printers.bambu import mqtt

    factory = Mock()
    monkeypatch.setattr(mqtt, "BambuMQTTClient", factory)
    on_state = Mock()
    BambuPrinterPlugin().create_connection(config("bambu"), on_state_change=on_state)
    factory.assert_called_once_with(
        ip_address="192.0.2.21",
        serial_number="CaseSensitive",
        access_code="secret",
        model="sample",
        on_state_change=on_state,
    )


def test_bambu_declared_commands_exist_on_connection():
    from backend.app.printers.bambu.mqtt import BambuMQTTClient

    assert all(callable(getattr(BambuMQTTClient, name, None)) for name in BambuPrinterPlugin.operations)


def test_generic_models_use_installed_catalog_and_capabilities(isolated_registry):
    from backend.app.printers import models
    from backend.app.printers.capabilities import supports_chamber_heater

    plugin = ExamplePrinterPlugin()
    plugin.models = (
        PrinterModel(
            "sample",
            "Sample",
            aliases=("EX-1",),
            capabilities=frozenset({"dual_nozzle", "chamber_heater"}),
            gcode_compatibility_group="example-group",
        ),
        PrinterModel("sample2", "Sample 2", gcode_compatibility_group="example-group"),
    )
    isolated_registry.register(plugin)
    assert models.normalize_printer_model_id("EX-1") == "sample"
    assert models.is_dual_nozzle_model("EX-1") is True
    assert supports_chamber_heater("EX-1") is True
    assert models.is_gcode_compatible("sample", "sample2") is True
    assert models.is_gcode_compatible("sample", "X1C") is False
    assert models.normalize_printer_model("Bambu Lab X1 Carbon") == "X1C"
    assert models.is_dual_nozzle_model("X2D") is True


def test_missing_addon_binding_fails_closed(isolated_registry):
    files.bind_printer(config("missing"))
    with pytest.raises(UnknownPrinterPlugin):
        api.plugin_for_ip("192.0.2.21")


def test_addon_id_accepts_package_namespaces(isolated_registry):
    plugin = ExamplePrinterPlugin()
    plugin.id = "org.example-printer"
    isolated_registry.register(plugin)
    assert isolated_registry.get("org.example-printer") is plugin


def test_connection_adapter_uses_handlers_without_raw_command_methods(isolated_registry):
    calls = []
    plugin = ExamplePrinterPlugin()
    plugin.operations = ("pause_print", "set_print_speed")
    plugin.command_handlers = lambda connection: {
        "pause_print": lambda: calls.append("paused") or True,
        "set_print_speed": lambda mode: calls.append(mode) or True,
    }
    isolated_registry.register(plugin)
    raw = SimpleNamespace(
        state=PrinterState(connected=True),
        connect=Mock(),
        disconnect=Mock(),
        check_staleness=lambda: True,
    )
    connection = api.wrap_connection(config(), raw)
    assert connection.pause_print() is True
    assert api.printer_api.execute(config(), connection, "set_print_speed", {"mode": 1}) is True
    assert connection.set_print_speed(2) is True
    assert connection.set_print_speed(mode=3) is True
    assert calls == ["paused", 1, 2, 3]
    assert connection.state is raw.state
    assert connection.check_staleness() is True
    assert connection.logging_enabled is False
    assert connection.last_connect_error is None
    assert getattr(connection, "optional_metadata", None) is None
    assert not hasattr(connection, "optional_metadata")
    with pytest.raises(UnsupportedPrinterOperation):
        connection.set_fan_speed(1, 100)
    connection.connect()
    connection.disconnect(timeout=0.5)
    raw.connect.assert_called_once_with()
    raw.disconnect.assert_called_once_with(timeout=0.5)


def test_connection_adapter_rejects_undeclared_raw_methods(isolated_registry):
    plugin = ExamplePrinterPlugin()
    isolated_registry.register(plugin)
    action = Mock()
    raw = SimpleNamespace(state=PrinterState(), dangerous=action)
    connection = api.wrap_connection(config(), raw)
    with pytest.raises(UnsupportedPrinterOperation):
        connection.dangerous()
    action.assert_not_called()


def test_non_bambu_snapshot_never_interprets_vendor_raw_data(isolated_registry):
    import json

    from backend.app.printers.serialization import get_derived_status_name, printer_state_to_dict

    class VendorPayload(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Core interpreted vendor raw_data")

        def __contains__(self, key):
            raise AssertionError("Core interpreted vendor raw_data")

    plugin = ExamplePrinterPlugin()
    isolated_registry.register(plugin)
    api.bind_printer(config())
    state = PrinterState(
        connected=True,
        state="RUNNING",
        current_print="sample.gcode",
        progress=42,
        temperatures={"nozzle": 205, "bed": 55},
        status_name="Warming nozzle",
        filament_units=[{"id": 0, "temp": 22.5, "tray": []}],
        external_spools=[{"id": 7, "tray_type": "PLA"}],
        active_plate_id=3,
        raw_data=VendorPayload({"anything": "private vendor payload"}),
    )
    snapshot = printer_state_to_dict(state, 21, "sample")
    assert snapshot["progress"] == 42
    assert snapshot["stg_cur_name"] == "Warming nozzle"
    assert snapshot["ams"][0]["id"] == 0
    assert snapshot["vt_tray"][0]["id"] == 7
    assert snapshot["current_plate_id"] == 3
    assert snapshot["supports_drying"] is False
    assert "raw_data" not in snapshot
    assert "private vendor payload" not in json.dumps(snapshot)
    assert get_derived_status_name(state, "sample", plugin_id="example") == "Warming nozzle"
    assert api.printer_api.serialize_state(config(), state)["progress"] == 42


def test_plugin_can_supply_custom_normalized_snapshot(isolated_registry):
    from backend.app.printers.serialization import printer_state_to_dict

    plugin = ExamplePrinterPlugin()
    plugin.serialize_state = lambda state, printer_id, model, drying_targets: {
        "connected": state.connected,
        "state": state.state,
        "stg_cur_name": "Vendor ready",
        "progress": state.progress,
    }
    isolated_registry.register(plugin)
    snapshot = printer_state_to_dict(PrinterState(connected=True), model="sample", plugin_id="example")
    assert snapshot["stg_cur_name"] == "Vendor ready"
    assert snapshot["awaiting_plate_clear"] is False


def _normalized_plugin_state():
    class VendorPayload(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("Core interpreted vendor raw_data")

    return PrinterState(
        connected=True,
        state="IDLE",
        plugin_id="example",
        status_name="Ready",
        raw_data=VendorPayload({"secret": "private payload"}),
        filament_units=[{"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "state": 11}]}],
        external_spools=[{"id": 254, "tray_type": "PETG", "tray_color": "0000FFFF"}],
    )


def test_normalized_plugin_filaments_reach_scheduler_and_offline_history(isolated_registry, monkeypatch):
    from backend.app.printers.state_accessors import printer_telemetry
    from backend.app.services import print_scheduler as scheduler_module
    from backend.app.services.printer_manager import PrinterManager

    isolated_registry.register(ExamplePrinterPlugin())
    state = _normalized_plugin_state()
    manager = PrinterManager()
    manager._clients[21] = SimpleNamespace(state=state, disconnect=Mock(), check_staleness=lambda: True)
    api.bind_printer(config())
    scheduler = scheduler_module.PrintScheduler()
    monkeypatch.setattr(scheduler_module, "printer_manager", manager)
    loaded = scheduler._build_loaded_filaments(state)
    assert {item["type"] for item in loaded} == {"PLA", "PETG"}
    assert scheduler._get_missing_filament_types(21, ["PLA", "PETG"]) == []
    assert scheduler._count_override_color_matches(21, [{"type": "PLA", "color": "FF0000"}]) == 1
    assert printer_telemetry(state)["ams"] == state.filament_units
    manager.disconnect_printer(21)
    assert manager.last_known_trays(21) == {"ams": state.filament_units, "vt_tray": state.external_spools}
    assert scheduler._tray_reading(21) == manager.last_known_trays(21)


@pytest.mark.asyncio
async def test_normalized_plugin_updates_broadcast_without_vendor_payload(isolated_registry, monkeypatch):
    from unittest.mock import AsyncMock

    from backend.app import main

    isolated_registry.register(ExamplePrinterPlugin())
    state = _normalized_plugin_state()
    config_for_callback = config(printer_id=9021)
    api.bind_printer(config_for_callback)
    manager = Mock()
    manager.get_printer.return_value = None
    manager.get_model.return_value = "sample"
    manager.get_drying_targets.return_value = {}
    manager.is_awaiting_plate_clear.return_value = False
    websocket = SimpleNamespace(send_printer_status=AsyncMock())
    monkeypatch.setattr(main, "printer_manager", manager)
    monkeypatch.setattr(main, "ws_manager", websocket)
    monkeypatch.setattr(main, "spawn_background_task", lambda coro, **kwargs: coro.close())
    monkeypatch.setattr(main.kprofile_drift, "needs_check", lambda *args: False)
    monkeypatch.setattr(main.notify_live_activities, "observe", lambda *args: None)
    monkeypatch.setattr(main.pause_eta, "observe", lambda *args: None)
    main._last_status_broadcast.pop(9021, None)
    main._printer_last_connected.pop(9021, None)
    main._printer_reconciled_since_connect.pop(9021, None)
    await main.on_printer_status_change(9021, state)
    state.status_name = "Warming nozzle"
    await main.on_printer_status_change(9021, state)
    state.filament_units[0]["tray"][0]["tray_color"] = "00FF00FF"
    await main.on_printer_status_change(9021, state)
    await main.on_printer_status_change(9021, state)
    assert websocket.send_printer_status.await_count == 3
    assert websocket.send_printer_status.call_args.args[1]["stg_cur_name"] == "Warming nozzle"
    main._last_status_broadcast.pop(9021, None)
    main._printer_last_connected.pop(9021, None)
    main._printer_reconciled_since_connect.pop(9021, None)


@pytest.mark.asyncio
async def test_same_plugin_shared_address_requires_identity_and_preserves_options(isolated_registry):
    from backend.app.printers import camera

    class ConfigAwarePlugin(ExamplePrinterPlugin):
        async def file_operation_for(self, printer_config, operation, *args, **kwargs):
            self.validate_config(printer_config)
            return [{"printer_id": printer_config.printer_id, "port": printer_config.options["port"]}]

        async def camera_operation_for(self, printer_config, operation, *args, **kwargs):
            self.validate_config(printer_config)
            return str(printer_config.options["port"]).encode()

    isolated_registry.register(ConfigAwarePlugin())
    first = PrinterConfig("192.0.2.21", "one", "key-one", "sample", "example", 21, {"port": 8080})
    second = PrinterConfig("192.0.2.21", "two", "key-two", "sample", "example", 22, {"port": 9090})
    api.bind_printer(first)
    api.bind_printer(second)
    with pytest.raises(PrinterPluginError, match="Multiple printer"):
        api.config_for_ip("192.0.2.21", plugin_id="example")
    with pytest.raises(PrinterPluginError, match="Multiple printer"):
        await files.list_files_async("192.0.2.21", "key-one")
    with pytest.raises(PrinterPluginError, match="Multiple printer"):
        await camera.capture_camera_frame_bytes("192.0.2.21", "key-one", "sample")
    assert await files.list_files_async("192.0.2.21", "key-two", printer_id=22) == [{"printer_id": 22, "port": 9090}]
    assert await camera.capture_camera_frame_bytes("192.0.2.21", "key-one", "sample", printer_id=21) == b"8080"
    assert await api.printer_api.files(second, "list_files_async", "192.0.2.21", "key-two") == [
        {"printer_id": 22, "port": 9090}
    ]
    with pytest.raises(PrinterPluginError, match="does not match"):
        api.config_for_ip("192.0.2.22", printer_id=21)
    with pytest.raises(PrinterPluginError, match="does not match"):
        api.config_for_ip("192.0.2.21", plugin_id="bambu", printer_id=21)
    with pytest.raises(PrinterPluginError, match="no binding"):
        api.config_for_ip("192.0.2.21", printer_id=999)
    assert api.config_for_ip("192.0.2.100", printer_id=999) is None

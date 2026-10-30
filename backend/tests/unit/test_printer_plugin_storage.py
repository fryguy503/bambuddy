"""Addon storage policy is independent of Bambu URLs, SD cards and FTP roots."""

from types import SimpleNamespace

import pytest

from backend.app.printers import api, storage
from backend.app.printers.contracts import PrinterConfig, PrinterPlugin, UnknownPrinterPlugin
from backend.app.printers.registry import PrinterPluginRegistry


class StorageAddon(PrinterPlugin):
    id = "storage_fixture"
    name = "Storage fixture"

    def create_connection(self, config, **callbacks):
        return None


@pytest.fixture
def storage_addon(monkeypatch):
    from backend.app.printers import registry as registry_module

    registry = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(api, "_bindings", {})
    addon = StorageAddon()
    registry.register(addon)
    return addon


def test_generic_storage_ignores_unrelated_wire_fields(storage_addon):
    state = SimpleNamespace(
        plugin_id=storage_addon.id,
        sdcard_reported=True,
        sdcard=False,
        current_project_url="brtc://emmc/cube.gcode.3mf",
    )
    assert storage.print_file_reachable_over_ftp(state).reachable is True
    assert storage.external_storage_present(state) is True
    assert storage.url_is_external_storage("http://fixture/jobs/cube.gcode", plugin_id=storage_addon.id) is None
    assert storage.ftp_probe_paths("cube.gcode", plugin_id=storage_addon.id) == ["cube.gcode"]


def test_provider_metadata_controls_reachability_and_probe_paths(storage_addon):
    state = SimpleNamespace(
        plugin_id=storage_addon.id,
        storage={
            "reachable": False,
            "reason": "provider_file_missing",
            "probe_filename": "cube.gcode",
            "probe_paths": ["jobs/cube.gcode", "history/cube.gcode"],
            "external_present": False,
        },
    )
    verdict = storage.print_file_reachable_over_ftp(state)
    assert verdict == storage.StorageVerdict(False, "provider_file_missing", "cube.gcode")
    assert storage.external_storage_present(state) is False
    assert storage.ftp_probe_paths("cube.gcode", state=state) == ["jobs/cube.gcode", "history/cube.gcode"]


def test_saved_printer_binding_selects_storage_addon_without_live_state(storage_addon):
    api.bind_printer(PrinterConfig("192.0.2.86", "fixture", "private", plugin_id=storage_addon.id, printer_id=8601))
    state = SimpleNamespace(sdcard_reported=True, sdcard=False)
    assert storage.print_file_reachable_over_ftp(state, printer_id=8601).reachable is True
    assert storage.ftp_probe_paths("cube.gcode", printer_id=8601) == ["cube.gcode"]


def test_storage_hook_receives_printer_configuration(storage_addon, monkeypatch):
    config = PrinterConfig("192.0.2.86", "fixture", "private", plugin_id=storage_addon.id, printer_id=8601)
    api.bind_printer(config)
    calls = []

    def policy(printer, operation, *args, **kwargs):
        calls.append((printer, operation))
        return storage.StorageVerdict(False, "configured_policy")

    monkeypatch.setattr(storage_addon, "storage_operation_for", policy)
    assert storage.print_file_reachable_over_ftp(None, printer_id=8601).reason == "configured_policy"
    assert calls == [(config, "print_file_reachable_over_ftp")]


def test_missing_storage_addon_fails_without_bambu_fallback(storage_addon):
    state = SimpleNamespace(plugin_id="removed_addon", sdcard_reported=True, sdcard=False)
    with pytest.raises(UnknownPrinterPlugin):
        storage.print_file_reachable_over_ftp(state)


@pytest.mark.parametrize("filename", ["../cube.gcode", "C:\\cube.gcode", ".hidden", "cube\ngcode"])
def test_normalized_probe_filename_cannot_escape_local_temp_directory(storage_addon, filename):
    state = SimpleNamespace(plugin_id=storage_addon.id, storage={"reachable": False, "probe_filename": filename})
    assert storage.print_file_reachable_over_ftp(state).probe_filename is None

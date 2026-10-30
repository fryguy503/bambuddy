"""Durable provider pins retain identity without persisting device credentials."""

import json

import pytest

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.management import PluginManager, printer_pin_identity
from backend.app.printers.packages import PluginPackageError
from backend.tests.unit.test_printer_plugin_packages import package_bytes


def config(serial="PRIVATE-SERIAL"):
    return PrinterConfig(
        "192.0.2.123",
        serial,
        "PRIVATE-ACCESS-CODE",
        plugin_id="example",
        printer_id=7,
        options={"token": "PRIVATE-TOKEN"},
    )


def test_pin_survives_reload_without_credentials_and_accepts_legacy_state(tmp_path):
    manager = PluginManager(tmp_path)
    assert manager.get_printer_pins() == {}
    manager.set_printer_pin(config(), "1.0.0")
    reloaded = PluginManager(tmp_path)
    assert reloaded.get_printer_pins() == {
        "7": {"plugin_id": "example", "version": "1.0.0", "identity": printer_pin_identity(config())}
    }
    text = manager.state_path.read_text()
    for secret in ("PRIVATE-SERIAL", "192.0.2.123", "PRIVATE-ACCESS-CODE", "PRIVATE-TOKEN"):
        assert secret not in text
    assert json.loads(text)["format_version"] == 1


def test_reassigned_pin_cannot_be_removed_by_a_stale_cleanup(tmp_path):
    manager = PluginManager(tmp_path)
    manager.set_printer_pin(config(), "1.0.0")
    before = manager.get_printer_pins()["7"]
    manager.set_printer_pin(config("NEW-DEVICE"), "2.0.0")
    after = manager.get_printer_pins()["7"]
    assert before["identity"] != after["identity"]
    assert not manager.remove_printer_pin(7, expected=before)
    assert manager.get_printer_pins()["7"] == after
    assert manager.remove_printer_pin(7, expected=after)
    assert not manager.remove_printer_pin(7)


@pytest.mark.parametrize("change", [{"identity": "broken"}, {"version": ""}, {"access_code": "secret"}])
def test_corrupt_pins_fail_closed(tmp_path, change):
    manager = PluginManager(tmp_path)
    manager.set_printer_pin(config(), "1.0.0")
    state = manager.read_state()
    state["printer_pins"]["7"].update(change)
    manager._write_state(state)
    with pytest.raises(PluginPackageError, match="selection state cannot be read"):
        manager.get_printer_pins()


def test_retained_old_version_and_runtime_references_cannot_be_removed(tmp_path):
    manager = PluginManager(tmp_path)
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        manager.stage(package_bytes(version=version))
    manager.select("example", "2.0.0", loaded_version="1.0.0")
    manager.select("example", "3.0.0", loaded_version="2.0.0")
    manager.set_printer_pin(config(), "1.0.0")
    with pytest.raises(PluginPackageError, match="cannot be removed"):
        manager.remove_version("example", "1.0.0")
    manager.remove_printer_pin(7)
    with pytest.raises(PluginPackageError, match="cannot be removed"):
        manager.remove_version("example", "1.0.0", loaded_versions={"1.0.0"})
    assert manager.remove_version("example", "1.0.0")["removed"]


def test_selection_rollback_preserves_newly_staged_packages(tmp_path):
    manager = PluginManager(tmp_path)
    manager.stage(package_bytes())
    manager.stage(package_bytes(version="2.0.0"))
    manager.select("example", "1.0.0")
    checkpoint = manager.selection_checkpoint("example")
    assert manager.select("example", "2.0.0", loaded_version="1.0.0", live=True)["restart_required"] is False
    manager.stage(package_bytes(version="3.0.0"))
    manager.restore_selection("example", checkpoint, expected_version="2.0.0")
    assert manager.selection_checkpoint("example") == checkpoint
    assert "3.0.0" in manager.read_state()["plugins"]["example"]["versions"]
    with pytest.raises(PluginPackageError, match="selection changed"):
        manager.restore_selection("example", checkpoint, expected_version="2.0.0")

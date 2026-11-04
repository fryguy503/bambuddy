"""Remaining-balance edits keep their selected physical spool and are opt-in."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import OperationRequest, SDKValidationError, validate_device, validate_snapshot
from plugins.anycubic.payload import inventory
from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

SOURCE = "material:ace:0:0"


@pytest.fixture
def linked():
    plugin = AnycubicPrinterPlugin()
    config = PrinterConfig(
        "192.168.50.50",
        "REMAINING-CONTROLS",
        "12345678",
        model="Kobra S1",
        plugin_id="anycubic",
        options={"tls_fingerprint": "a" * 64},
    )
    connection = plugin.create_connection(config)
    connection.inventory_links = {SOURCE: {"spool_id": 17, "token": "same-spool", "identity": []}}
    return SimpleNamespace(plugin=plugin, config=config, connection=connection)


def test_remaining_actions_pin_assigned_spool_and_require_inventory_permission(linked):
    device = validate_device(linked.plugin.describe_device(linked.config))
    for name in ("sync_remaining", "set_remaining_sync"):
        action = device.operation("anycubic.inventory." + name)
        assert action.permission == "inventory:update"
        assert action.resource_ids == [SOURCE] and not action.resource_kinds
        field = action.resource_parameters_schemas[SOURCE]["properties"]["expected_spool_id"]
        assert field["const"] == field["default"] == 17
    assert device.operation("anycubic.inventory.sync_remaining").confirmation
    assert (
        device.operation("anycubic.inventory.set_remaining_sync").resource_parameters_schemas[SOURCE]["properties"][
            "enabled"
        ]["default"]
        is False
    )
    state = validate_snapshot(device, linked.connection.state)
    source = next(item for item in state.resources if item.resource_id == SOURCE)
    assert source.properties["auto_remaining_sync"].value is False


def test_unassigned_slots_offer_assignment_before_remaining_actions(linked):
    linked.connection.inventory_links = {}
    device = linked.plugin.describe_device(linked.config)
    assert "anycubic.inventory.assign" in {item.id for item in device.operations}
    assert not any(item.id.endswith(("sync_remaining", "set_remaining_sync")) for item in device.operations)


@pytest.mark.asyncio
async def test_confirmed_remaining_action_uses_only_the_pinned_assignment(linked, monkeypatch):
    sync = AsyncMock(return_value={"state": "synced", "remaining_grams": 710})
    monkeypatch.setattr(inventory, "sync_remaining", sync)
    result = await linked.connection.invoke(
        OperationRequest(
            operation_id="anycubic.inventory.sync_remaining",
            resource_id=SOURCE,
            parameters={"expected_spool_id": 17},
        )
    )
    assert result.status == "succeeded" and result.result["remaining_grams"] == 710
    sync.assert_awaited_once_with(linked.config, SOURCE, linked.connection._remaining_sample, expected_spool_id=17)
    linked.connection.inventory_links[SOURCE]["spool_id"] = 18
    with pytest.raises(SDKValidationError):
        await linked.connection.invoke(
            OperationRequest(
                operation_id="anycubic.inventory.sync_remaining",
                resource_id=SOURCE,
                parameters={"expected_spool_id": 17},
            )
        )
    assert sync.await_count == 1


@pytest.mark.asyncio
async def test_auto_preference_is_an_explicit_inventory_operation_and_updates_descriptor(linked, monkeypatch):
    before = linked.plugin.describe_device(linked.config)
    updated = {
        SOURCE: {
            **linked.connection.inventory_links[SOURCE],
            "auto_remaining_sync": True,
            "auto_remaining_token": "same-spool",
        }
    }
    save = AsyncMock(return_value=updated)
    monkeypatch.setattr(inventory, "set_remaining_sync", save)
    linked.connection._emit = MagicMock()
    result = await linked.connection.invoke(
        OperationRequest(
            operation_id="anycubic.inventory.set_remaining_sync",
            resource_id=SOURCE,
            parameters={"expected_spool_id": 17, "enabled": True},
        )
    )
    assert result.status == "succeeded" and result.result == {"enabled": True}
    save.assert_awaited_once_with(linked.config, SOURCE, True, 17)
    after = linked.plugin.describe_device(linked.config)
    assert after.revision != before.revision
    assert (
        after.operation("anycubic.inventory.set_remaining_sync").resource_parameters_schemas[SOURCE]["properties"][
            "enabled"
        ]["default"]
        is True
    )


@pytest.mark.asyncio
async def test_background_remaining_sync_is_disabled_by_default_and_throttled_when_selected(linked, monkeypatch):
    from plugins.anycubic.payload import connection as connection_module, history

    pending = []
    current = [100.0]
    linked.connection._schedule = pending.append
    linked.connection._observe_jobs = MagicMock()
    linked.connection._emit = MagicMock()
    linked.connection.native.ace_material_snapshot = MagicMock(return_value=[])
    monkeypatch.setattr(connection_module, "time", SimpleNamespace(monotonic=lambda: current[0]))
    monkeypatch.setattr(inventory, "observe", AsyncMock())
    monkeypatch.setattr(inventory, "snapshot", AsyncMock(side_effect=lambda _: linked.connection.inventory_links))
    automatic = AsyncMock(return_value=[])
    monkeypatch.setattr(inventory, "sync_remaining_auto", automatic)
    monkeypatch.setattr(history, "record", MagicMock())

    async def report():
        linked.connection._changed()
        assert len(pending) == 1
        await pending.pop()

    await report()
    automatic.assert_not_awaited()
    linked.connection.inventory_links[SOURCE]["auto_remaining_sync"] = True
    linked.connection.inventory_links[SOURCE]["auto_remaining_token"] = "same-spool"
    await report()
    automatic.assert_awaited_once_with(linked.config, linked.connection._remaining_sample)
    await report()
    assert automatic.await_count == 1
    current[0] += 31
    await report()
    assert automatic.await_count == 2

"""Native ACE inventory authority without printer traffic or physical hardware."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.anycubic import AnycubicMQTTClient
from backend.app.services.anycubic.protocol import PREFIX


def box(identity=0):
    return {
        "id": identity,
        "slots": [
            {"index": index, "status": 5, "edit_status": 0, "type": "PLA Matte", "color": [17, 34, 51]}
            for index in range(4)
        ],
    }


@pytest.fixture
def client():
    value = AnycubicMQTTClient("192.168.1.50", "fixture", tls_fingerprint="a" * 64)
    value._client = MagicMock()
    value._device_id = "fixture"
    value._subscribed = True
    return value


def receive(client, boxes, action="getInfo", **extra):
    message = SimpleNamespace(
        topic=f"{PREFIX}/printer/public/20025/fixture/multiColorBox/report",
        payload=json.dumps(
            {"type": "multiColorBox", "action": action, "code": 200, "data": {"multi_color_box": boxes}, **extra}
        ).encode(),
        retain=False,
    )
    client._received(client._generation, client._client, message)


def test_recent_complete_inventory_is_sorted_detached_and_never_publishes(client):
    receive(client, [box(1), box(0)])
    snapshot = client.ace_material_snapshot()
    assert snapshot is not None
    assert [unit["id"] for unit in snapshot] == [0, 1]
    assert snapshot[0]["tray"][0]["tray_type"] == "PLA Matte"
    assert snapshot[0]["tray"][0]["tray_color"] == "112233FF"
    snapshot[0]["tray"][0]["tray_type"] = "ABS"
    snapshot.clear()
    assert client.ace_material_snapshot()[0]["tray"][0]["tray_type"] == "PLA Matte"
    client._client.publish.assert_not_called()


@pytest.mark.parametrize("field", ["_subscribed", "_device_id", "_client"])
def test_no_active_mqtt_session_has_no_inventory_authority(client, field):
    receive(client, [box()])
    setattr(client, field, None)
    assert client.ace_material_snapshot() is None


def test_disconnected_callback_discards_previous_inventory_authority(client):
    receive(client, [box()])
    client._disconnected(client._generation, client._client)
    assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {}


def test_recent_temperature_messages_cannot_renew_old_materials(client):
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=100):
        receive(client, [box()])
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=161):
        client._last_message_monotonic = 161
        assert client.ace_material_snapshot() is None


def test_partial_slot_or_drying_updates_cannot_renew_complete_material_authority(client):
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=100):
        receive(client, [box()])
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=159):
        receive(client, [{"id": 0, "slots": [{"index": 0, "consumables_percent": 75}]}], action="refresh")
        receive(client, [{"id": 0, "drying_status": {"status": 0}}], action="refresh")
        assert client.ace_material_snapshot() is not None
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=161):
        client._last_message_monotonic = 161
        assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {0: 100}


def test_each_known_box_requires_recent_complete_materials(client):
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=100):
        receive(client, [box(0), box(1)])
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=159):
        receive(client, [box(0)], action="refresh")
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=161):
        client._last_message_monotonic = 161
        assert client.ace_material_snapshot() is None


@pytest.mark.parametrize("field,value", [("type", "ABS"), ("color", [70, 80, 90]), ("edit_status", 1), ("status", 4)])
def test_partial_material_changes_invalidate_old_tuple_until_complete_inventory(client, field, value):
    receive(client, [box()])
    receive(client, [{"id": 0, "slots": [{"index": 0, field: value}]}], action="refresh")
    assert client.ace_material_snapshot() is None
    receive(client, [box()])
    assert client.ace_material_snapshot() is not None


@pytest.mark.parametrize(
    "field,value", [("status", 4.0), ("type", "P" * 65), ("type", "PLA\nMatte"), ("type", "PLA\x00")]
)
def test_invalid_four_slot_partial_refresh_cannot_restore_material_authority(client, field, value):
    receive(client, [box()])
    receive(client, [{"id": 0, "slots": [{"index": 0, "type": "ABS"}]}], action="refresh")
    assert client.ace_material_snapshot() is None
    invalid = box()
    invalid["slots"][0][field] = value
    receive(client, [invalid], action="refresh")
    assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {}


def test_incomplete_new_unit_never_becomes_inventory(client):
    receive(client, [{"id": 1, "slots": [box(1)["slots"][0]]}], action="refresh")
    assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {}
    assert client._telemetry.boxes == {}


def test_complete_slot_addresses_without_complete_material_fields_are_not_authoritative(client):
    receive(client, [{"id": 0, "slots": [{"index": index, "status": 5} for index in range(4)]}])
    assert client.ace_material_snapshot() is None


@pytest.mark.parametrize("missing", ["type", "color", "edit_status"])
def test_missing_field_cannot_inherit_fresh_material_authority_on_full_refresh(client, missing):
    receive(client, [box()])
    incomplete = box()
    del incomplete["slots"][0][missing]
    receive(client, [incomplete])
    assert client.ace_material_snapshot() is None


def test_explicit_empty_slot_needs_no_material_fields(client):
    empty = box()
    empty["slots"][0] = {"index": 0, "status": 4}
    receive(client, [empty])
    snapshot = client.ace_material_snapshot()
    assert snapshot is not None
    assert snapshot[0]["tray"][0]["state"] == 9
    assert snapshot[0]["tray"][0]["tray_type"] == ""


def test_reconnect_clears_authority_before_worker_starts(client):
    receive(client, [box()])
    with (
        patch("backend.app.services.anycubic.client.retire_paho_client"),
        patch("backend.app.services.anycubic.client.threading.Thread") as worker,
    ):
        client.connect()
    worker.return_value.start.assert_called_once()
    assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {}


def test_staleness_drops_box_authority_and_later_partial_update_cannot_restore_it(client):
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=100):
        receive(client, [box()])
    with patch("backend.app.services.anycubic.client.time.monotonic", return_value=161):
        assert client.check_staleness()
        receive(client, [{"id": 0, "slots": [box()["slots"][0]]}], action="refresh")
    assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {}

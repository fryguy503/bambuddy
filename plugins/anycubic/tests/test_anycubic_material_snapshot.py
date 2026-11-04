"""Native ACE inventory authority without printer traffic or physical hardware."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugins.anycubic.payload.client import AnycubicMQTTClient
from plugins.anycubic.payload.protocol import PREFIX


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
        topic=f"{PREFIX}/printer/public/{client._model_id}/fixture/multiColorBox/report",
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
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=100):
        receive(client, [box()])
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=161):
        client._last_message_monotonic = 161
        assert client.ace_material_snapshot() is None


def test_partial_slot_or_drying_updates_cannot_renew_complete_material_authority(client):
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=100):
        receive(client, [box()])
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=159):
        receive(client, [{"id": 0, "slots": [{"index": 0, "consumables_percent": 75}]}], action="refresh")
        receive(client, [{"id": 0, "drying_status": {"status": 0}}], action="refresh")
        assert client.ace_material_snapshot() is not None
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=161):
        client._last_message_monotonic = 161
        assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {0: 100}


def test_each_known_box_requires_recent_complete_materials(client):
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=100):
        receive(client, [box(0), box(1)])
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=159):
        receive(client, [box(0)], action="refresh")
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=161):
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
        patch("plugins.anycubic.payload.client.retire_paho_client"),
        patch("plugins.anycubic.payload.client.threading.Thread") as worker,
    ):
        client.connect()
    worker.return_value.start.assert_called_once()
    assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {}


def test_staleness_drops_box_authority_and_later_partial_update_cannot_restore_it(client):
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=100):
        receive(client, [box()])
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=161):
        assert not client.check_staleness()
        receive(client, [{"id": 0, "slots": [box()["slots"][0]]}], action="refresh")
    assert client.ace_material_snapshot() is None
    assert client._ace_material_seen == {}


def remaining_box(identity=0, remaining=71):
    value = box(identity)
    for slot in value["slots"]:
        slot["consumables_percent"] = remaining
    return value


def test_remaining_requires_its_own_recent_report_even_when_materials_are_fresh(client):
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=100):
        receive(client, [remaining_box()])
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=159):
        receive(client, [box()], action="refresh")
        assert client.ace_remaining_snapshot()[0]["tray"][0]["remain"] == 71
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=161):
        client._last_message_monotonic = 161
        assert "remain" not in client.ace_remaining_snapshot()[0]["tray"][0]
        assert client.ace_material_snapshot()[0]["tray"][0]["remain"] == 71
    assert client._telemetry.remaining_observed_at == {(0, index): 100 for index in range(4)}
    client._client.publish.assert_not_called()


@pytest.mark.parametrize("remaining", [0, 0.0, 42.5, 100])
def test_explicit_finite_remaining_preserves_zero_and_fractional_percent(client, remaining):
    receive(client, [remaining_box(remaining=remaining)])
    assert client.ace_remaining_snapshot()[0]["tray"][0]["remain"] == remaining
    assert set(client._telemetry.remaining_observed_at) == {(0, index) for index in range(4)}


@pytest.mark.parametrize("remaining", [True, False, None, "71", -1, 101])
def test_explicit_invalid_percent_discards_previous_balance_authority(client, remaining):
    receive(client, [remaining_box()])
    receive(client, [{"id": 0, "slots": [{"index": 0, "consumables_percent": remaining}]}], action="refresh")
    snapshot = client.ace_remaining_snapshot()
    assert snapshot is not None
    assert "remain" not in snapshot[0]["tray"][0]
    assert (0, 0) not in client._telemetry.remaining_observed_at
    assert snapshot[0]["tray"][1]["remain"] == 71


def test_drying_only_and_missing_partial_percent_do_not_renew_percentage(client):
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=100):
        receive(client, [remaining_box()])
    with patch("plugins.anycubic.payload.client.time.monotonic", return_value=130):
        receive(client, [{"id": 0, "drying_status": {"status": 0}}], action="refresh")
        receive(client, [{"id": 0, "slots": [{"index": 0}]}], action="refresh")
    assert client._telemetry.remaining_observed_at == {(0, index): 100 for index in range(4)}


@pytest.mark.parametrize(
    "field,value",
    [("type", "ABS"), ("color", [70, 80, 90]), ("edit_status", 1), ("status", 4)],
)
def test_replaced_material_cannot_inherit_previous_percentage(client, field, value):
    receive(client, [remaining_box()])
    receive(client, [{"id": 0, "slots": [{"index": 0, field: value}]}], action="refresh")
    assert (0, 0) not in client._telemetry.remaining_observed_at
    assert "remain" not in client._telemetry.boxes[0]["tray"][0]
    replacement = box()
    replacement["slots"][0][field] = value
    receive(client, [replacement], action="refresh")
    assert "remain" not in client.ace_remaining_snapshot()[0]["tray"][0]


def test_full_inventory_without_percent_removes_prior_remaining_values(client):
    receive(client, [remaining_box(0), remaining_box(1)])
    receive(client, [box(1)])
    assert client._telemetry.remaining_observed_at == {}
    assert [item["id"] for item in client.ace_remaining_snapshot()] == [1]
    assert all("remain" not in tray for tray in client.ace_remaining_snapshot()[0]["tray"])


def test_remaining_snapshot_is_detached_and_keeps_native_box_slot_addresses(client):
    receive(client, [remaining_box(1, 33), remaining_box(0, 71)])
    snapshot = client.ace_remaining_snapshot()
    assert [(unit["id"], tray["id"], tray["remain"]) for unit in snapshot for tray in unit["tray"]] == [
        (identity, slot, remaining) for identity, remaining in ((0, 71), (1, 33)) for slot in range(4)
    ]
    snapshot[0]["tray"][0]["remain"] = 1
    snapshot[0]["tray"].clear()
    assert client.ace_remaining_snapshot()[0]["tray"][0]["remain"] == 71
    client._client.publish.assert_not_called()


@pytest.mark.parametrize("model,identity", [("Kobra S1 Max", 3), ("Kobra X", -1)])
def test_remaining_snapshot_preserves_model_specific_native_unit_addresses(model, identity):
    value = AnycubicMQTTClient("192.168.1.50", "fixture", model=model, tls_fingerprint="a" * 64)
    value._client, value._device_id, value._subscribed = MagicMock(), "fixture", True
    receive(value, [remaining_box(identity, 0)])
    snapshot = value.ace_remaining_snapshot()
    assert snapshot is not None and snapshot[0]["id"] == identity
    assert [(tray["id"], tray["remain"]) for tray in snapshot[0]["tray"]] == [(index, 0) for index in range(4)]
    assert set(value._telemetry.remaining_observed_at) == {(identity, index) for index in range(4)}
    value._client.publish.assert_not_called()


def test_unsupported_or_incomplete_packet_cannot_partially_commit_remaining_authority(client):
    receive(client, [remaining_box()])
    before_boxes = deepcopy(client._telemetry.boxes)
    before_remaining = dict(client._telemetry.remaining_observed_at)
    invalid = remaining_box(1)
    invalid["slots"].append(dict(invalid["slots"][0]))
    receive(client, [remaining_box(0, 25), invalid])
    assert client._telemetry.boxes == before_boxes
    assert client._telemetry.remaining_observed_at == before_remaining


def test_new_mqtt_session_cannot_reuse_old_percent_timestamps(client):
    receive(client, [remaining_box()])
    with (
        patch("plugins.anycubic.payload.client.retire_paho_client"),
        patch("plugins.anycubic.payload.client.threading.Thread"),
    ):
        client.connect()
    assert client._telemetry.remaining_observed_at == {}
    assert client.ace_remaining_snapshot() is None

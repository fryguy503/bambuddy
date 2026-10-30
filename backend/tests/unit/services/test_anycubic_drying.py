"""ACE heater commands are synthetic fixtures; never actuate real hardware."""

import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.anycubic import AnycubicMQTTClient
from backend.app.services.anycubic.drying import limits
from backend.app.services.printer_manager import printer_state_to_dict


def dryer(state="IDLE", material="PETG", model_id=40002):
    client = AnycubicMQTTClient("192.168.1.50", "fixture", tls_fingerprint="a" * 64)
    envelope = {
        "action": "getInfo",
        "data": {
            "multi_color_box": [
                {
                    "id": identity,
                    "model_id": model_id,
                    "drying_status": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
                    "slots": [{"index": slot, "status": 5, "edit_status": 1, "type": material} for slot in range(4)],
                }
                for identity in (0, 1)
            ]
        },
    }
    client._telemetry.apply("multiColorBox", envelope)
    client._observe_ace_drying(envelope)
    client.state.connected, client.state.state = True, state
    client._last_message_monotonic = client._last_state_time = time.monotonic()
    client._publish = MagicMock(return_value=True)
    return client


@pytest.mark.parametrize("identity", [0, 1])
@pytest.mark.parametrize("state", ["IDLE", "RUNNING", "PAUSE"])
def test_drying_uses_physical_box_id_and_minutes_without_bambu_commands(identity, state):
    client = dryer(state)
    assert client.set_ace_drying(identity, temperature=60, duration_minutes=245)
    assert [call.args for call in client._publish.call_args_list] == [
        (
            "multiColorBox",
            "setDry",
            {"multi_color_box": [{"id": identity, "drying_status": {"status": 1, "target_temp": 60, "duration": 245}}]},
        ),
        ("multiColorBox", "getInfo", None),
    ]
    assert client.state.raw_data["ams"][identity]["anycubic_drying"]["status"] == 0


def test_stop_has_no_temperature_duration_or_print_state_requirement():
    client = dryer("FAILED")
    assert client.set_ace_drying(1, stop=True)
    assert client._publish.call_args_list[0].args == (
        "multiColorBox",
        "setDry",
        {"multi_color_box": [{"id": 1, "drying_status": {"status": 0}}]},
    )
    assert not client.set_ace_drying(1, stop=True, temperature=45)


@pytest.mark.parametrize(
    "temperature,duration", [(39, 240), (66, 240), (45, 4), (45, 1441), (True, 240), (45.5, 240), (45, True)]
)
def test_outside_native_bounds_is_rejected_before_publish(temperature, duration):
    client = dryer()
    assert not client.set_ace_drying(0, temperature=temperature, duration_minutes=duration)
    client._publish.assert_not_called()


@pytest.mark.parametrize("identity", [-1, 2, True])
def test_absent_boxes_and_builtin_feeder_cannot_dry(identity):
    client = dryer()
    assert not client.set_ace_drying(identity, temperature=45, duration_minutes=240)
    client._publish.assert_not_called()


def test_box_model_and_all_loaded_filaments_limit_the_whole_unit():
    client = dryer(material="PLA")
    assert not client.set_ace_drying(0, temperature=60, duration_minutes=240)
    assert client.set_ace_drying(0, temperature=55, duration_minutes=1440)
    client = dryer(material="PLA Matte")
    assert not client.set_ace_drying(0, temperature=65, duration_minutes=481)
    assert client.set_ace_drying(0, temperature=65, duration_minutes=480)
    assert client.set_ace_drying(0, temperature=60, duration_minutes=1440)
    assert limits({"anycubic_model_id": 40001, "tray": []})["temp_max"] == 55
    assert limits({"anycubic_model_id": 99999, "tray": []}) is None


@pytest.mark.parametrize("condition", ["stale-box", "missing-box", "disconnected", "preparing", "unknown-model"])
def test_missing_or_stale_capabilities_do_not_enable_heating(condition):
    client = dryer()
    if condition == "stale-box":
        client._ace_drying_seen[0] -= 120
    elif condition == "missing-box":
        client._ace_drying_seen.clear()
    elif condition == "disconnected":
        client.state.connected = False
    elif condition == "preparing":
        client.state.state = "PREPARE"
    else:
        client._telemetry.boxes[0]["anycubic_model_id"] = 99999
    assert not client.set_ace_drying(0, temperature=45, duration_minutes=240)
    client._publish.assert_not_called()


def test_partial_dryer_reports_preserve_sensors_zero_and_unrelated_units():
    client = dryer()
    assert client._telemetry.apply(
        "multiColorBox",
        {
            "action": "autoUpdateDryStatus",
            "data": {
                "multi_color_box": [{"id": 1, "drying_status": {"status": 1, "target_temp": 55, "remain_time": 0}}]
            },
        },
    )
    units = printer_state_to_dict(client.state, model="Kobra S1")["ams"]
    assert units[0]["anycubic_drying"]["status"] == 0
    assert units[1]["anycubic_drying"] == {
        "status": 1,
        "target_temp": 55,
        "duration_minutes": 0,
        "remaining_minutes": 0,
    }
    assert units[0]["tray"][0]["remain"] is None
    assert not client._telemetry.apply(
        "multiColorBox", {"action": "setDry", "data": {"multi_color_box": [{"id": 1, "drying_status": {"status": 0}}]}}
    )
    assert client._telemetry.boxes[1]["anycubic_drying"]["status"] == 1

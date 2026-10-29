"""Controls are protocol-only fixtures: never change a real printer setting."""

import time
from unittest.mock import MagicMock

import pytest

from backend.app.services.anycubic import AnycubicMQTTClient


def client(state="RUNNING", model="Kobra S1"):
    value = AnycubicMQTTClient("192.168.1.50", "test", model=model, tls_fingerprint="a" * 64)
    value.state.connected, value.state.state = True, state
    value._last_message_monotonic = value._last_state_time = time.monotonic()
    value._telemetry.task_id = "active-task"
    value._publish = MagicMock(return_value=True)
    return value


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE"])
def test_printing_heater_changes_use_update_without_gcode_or_start(state):
    value = client(state)
    assert value.set_nozzle_temperature(220)
    assert value.set_bed_temperature(60)
    assert [call.args for call in value._publish.call_args_list] == [
        ("print", "update", {"taskid": "active-task", "settings": {"target_nozzle_temp": 220}}),
        ("print", "update", {"taskid": "active-task", "settings": {"target_hotbed_temp": 60}}),
    ]


def test_idle_heater_uses_native_type_and_zero_for_unselected_heater():
    value = client("IDLE")
    assert value.set_nozzle_temperature(0)
    assert value.set_bed_temperature(55)
    assert [call.args for call in value._publish.call_args_list] == [
        ("tempature", "set", {"type": 0, "target_nozzle_temp": 0, "target_hotbed_temp": 0}),
        ("tempature", "set", {"type": 1, "target_nozzle_temp": 0, "target_hotbed_temp": 55}),
    ]


@pytest.mark.parametrize("bad", [-1, 321, True, 210.5])
def test_temperature_rejects_invalid_targets_and_bambu_second_nozzle(bad):
    value = client()
    assert not value.set_nozzle_temperature(bad)
    assert not value.set_nozzle_temperature(210, nozzle=1)
    value._publish.assert_not_called()


def test_reported_lower_limits_and_idle_model_limits_are_enforced():
    value = client(model="Kobra 3 Max")
    assert not value.set_nozzle_temperature(261)
    assert not value.set_bed_temperature(101)
    value.state.state = "IDLE"
    assert not value.set_bed_temperature(91)
    value._control_report("info", {"data": {"free_temp_limit": {"nozzle_temp_limit": [0, 240]}}})
    assert not value.set_nozzle_temperature(241)
    assert value.set_nozzle_temperature(240)


@pytest.mark.parametrize("scale", [3, 100])
def test_enclosure_fan_uses_reported_scale_and_percentages_are_not_pwm(scale):
    value = client()
    value._control_report(
        "fan",
        {"data": {"fan_speed_pct": 0, "aux_fan_speed_pct": 0, "box_fan_level": 0, "box_fan_level_limit": [0, scale]}},
    )
    assert value.set_fan_percent("chamber", 100)
    assert value.set_fan_percent("part", 60)
    assert [call.args for call in value._publish.call_args_list] == [
        ("print", "update", {"taskid": "active-task", "settings": {"box_fan_level": scale}}),
        ("print", "update", {"taskid": "active-task", "settings": {"fan_speed_pct": 60}}),
    ]


def test_unreported_or_ambiguous_fan_is_not_controlled():
    value = client()
    value._control_report("fan", {"data": {"box_fan_level": 0}})
    assert not value.set_fan_percent("chamber", 100)
    assert not value.set_fan_percent("aux2", 60)
    value._control_report("fan", {"data": {"box_fan_level": 60}})
    assert value.set_fan_percent("chamber", 50)


@pytest.mark.parametrize("unavailable", ["stale", "no_task", "disconnected", "preparing"])
def test_unknown_or_stale_print_cannot_receive_settings(unavailable):
    value = client()
    if unavailable == "stale":
        value._last_state_time -= 120
    elif unavailable == "no_task":
        value._telemetry.task_id = None
    elif unavailable == "disconnected":
        value.state.connected = False
    else:
        value.state.state = "PREPARE"
    assert not value.set_nozzle_temperature(220)
    assert not value.set_print_speed(2)
    value._publish.assert_not_called()


def test_speed_rejects_bambu_only_fourth_mode():
    value = client()
    assert not value.set_print_speed(4)
    assert value.set_print_speed(3)
    value._publish.assert_called_once_with(
        "print", "update", {"taskid": "active-task", "settings": {"print_speed_mode": 3}}
    )

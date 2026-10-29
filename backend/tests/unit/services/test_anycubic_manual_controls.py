"""Manual controls use native payloads, fresh sessions and bounded actuation."""

import time
from unittest.mock import MagicMock, patch

import pytest

from backend.tests.unit.services.test_anycubic_drying import dryer


def manual(state="IDLE"):
    client = dryer(state)
    client._info_observed = time.monotonic()
    return client


@pytest.mark.parametrize("axis,distance,code,direction", [("x", -10, 1, 0), ("y", 50, 2, 1), ("z", 1, 3, 1)])
def test_jog_uses_native_axis_and_direction_with_absolute_distance(axis, distance, code, direction):
    client = manual()
    assert client.jog_axis(axis, distance)
    client._publish.assert_called_once_with(
        "axis", "move", {"axis": code, "move_type": direction, "distance": abs(distance)}
    )


@pytest.mark.parametrize("axis,code", [("xy", 4), ("z", 3), ("all", 5)])
def test_home_preserves_native_axis_choice(axis, code):
    client = manual()
    assert client.home_axes(axis)
    client._publish.assert_called_once_with("axis", "move", {"axis": code, "move_type": 2, "distance": 0})


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "unknown"])
def test_jog_home_and_slot_edits_cannot_run_during_a_print_or_unknown_state(state):
    client = manual(state)
    assert not client.jog_axis("x", 1)
    assert not client.home_axes()
    assert not client.set_ace_slot(0, 0, "PLA Matte", "FF0000")
    assert not client.feed_ace_slot(0, 0, "load")
    client._publish.assert_not_called()


@pytest.mark.parametrize("distance", [0, -51, 51, float("nan"), float("inf"), True, "10"])
def test_unbounded_or_untyped_moves_do_not_publish(distance):
    client = manual()
    assert not client.jog_axis("x", distance)
    client._publish.assert_not_called()


def test_light_requires_a_reported_fresh_chamber_light_but_can_run_while_printing():
    client = manual("RUNNING")
    assert not client.set_chamber_light(True)
    client._control_report("light", {"data": {"lights": [{"type": 2, "status": 0}]}})
    assert client.set_chamber_light(True)
    client._publish.assert_called_once_with("light", "control", {"type": 2, "status": 1, "brightness": 100})
    client._reported_lights[2] -= 120
    assert not client.set_chamber_light(False)


def test_extruder_pulse_requires_hot_idle_report_and_stops_on_the_same_connection():
    client = manual()
    client._control_report("extrudeControl", {"data": {"operate_type": 0}})
    client.state.temperatures["nozzle"] = 169
    assert not client.extruder_pulse(1)
    client.state.temperatures["nozzle"] = 200
    with patch("backend.app.services.anycubic.client.threading.Timer") as timer:
        assert client.extruder_pulse(1)
        timer.assert_called_once()
        assert timer.call_args.args[0] == 0.5
        assert client._extrusion_timer is timer.return_value
        assert not client.extruder_pulse(1)
        timer.call_args.args[1]()
    assert [call.args for call in client._publish.call_args_list] == [
        ("extrudeControl", "set", {"operate_type": 1, "move_type": 1}),
        ("extrudeControl", "set", {"operate_type": 0}),
        ("extrudeControl", "getInfo", None),
    ]
    assert not client.extruder_pulse(1)  # Wait for actual stop telemetry.


def test_old_pulse_timer_does_not_stop_a_replacement_connection():
    client = manual()
    client.state.temperatures["nozzle"] = 200
    client._control_report("extrudeControl", {"data": {"operate_type": 0}})
    with patch("backend.app.services.anycubic.client.threading.Timer") as timer:
        assert client.extruder_pulse(0)
        client._generation += 1
        timer.call_args.args[1]()
    assert client._publish.call_count == 1


def test_extrusion_receipt_cannot_confirm_that_pending_motion_stopped():
    client = manual()
    client._extrusion_pending = True
    client._extrude_operate = None
    client._control_report("extrudeControl", {"action": "set", "data": {"operate_type": 0}})
    assert client._extrusion_pending
    assert client._extrude_operate is None
    client._control_report("extrudeControl", {"action": "getInfo", "data": {"operate_type": 0}})
    assert not client._extrusion_pending
    assert client._extrude_operate == 0


def test_material_edits_keep_exact_variant_and_rgb_and_never_bambu_ids():
    client = manual()
    assert client.set_ace_slot(1, 3, "PLA Matte", "ff8000")
    assert client._publish.call_args_list[0].args == (
        "multiColorBox",
        "setInfo",
        {"multi_color_box": [{"id": 1, "slots": [{"index": 3, "type": "PLA Matte", "color": [255, 128, 0]}]}]},
    )
    client._telemetry.boxes[1]["tray"][3]["anycubic_edit_status"] = 0
    assert not client.set_ace_slot(1, 3, "PLA", "000000")
    assert not client.set_ace_slot(4, 3, "PLA", "000000")
    assert client._publish.call_count == 2  # One edit and one read query.


@pytest.mark.parametrize("operation,code", [("load", 1), ("unload", 2), ("stop", 3)])
def test_feed_operations_address_one_physical_slot(operation, code):
    client = manual()
    assert client.feed_ace_slot(1, 2, operation)
    client._publish.assert_called_once_with(
        "multiColorBox",
        "feedFilament",
        {"multi_color_box": [{"id": 1, "feed_status": {"slot_index": 2, "type": code}}]},
    )


def test_idle_speed_uses_native_mode_setting():
    client = manual()
    assert client.set_print_speed(3)
    assert [call.args for call in client._publish.call_args_list] == [
        ("print", "setSpeedMode", {"speed_mode": 3}),
        ("info", "query", None),
    ]


def test_cancel_of_own_manual_feed_remains_available_during_its_busy_phase():
    client = manual()
    assert client.feed_ace_slot(1, 2, "load")
    client.state.state = "BUSY"
    assert client.feed_ace_slot(1, 2, "stop")
    assert not client.feed_ace_slot(1, 1, "stop")
    client.state.state = "RUNNING"
    assert not client.feed_ace_slot(1, 2, "stop")


def test_reported_zero_chamber_fan_is_visible_without_guessing_its_scale():
    client = manual()
    client._control_report("fan", {"data": {"box_fan_level": 0}})
    assert client.state.big_fan2_speed == 0
    assert "chamber" in client.control_capabilities()["reported_fans"]
    assert "chamber" not in client.control_capabilities()["fans"]
    assert not client.set_fan_percent("chamber", 50)
    client._publish.assert_not_called()

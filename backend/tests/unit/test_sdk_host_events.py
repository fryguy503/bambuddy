"""Typed events enter host workflows without vendor protocol dictionaries."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import (
    DeviceEvent,
    JobArtifact,
    JobEventData,
    JobProgress,
    SDKValidationError,
    validate_snapshot,
)
from backend.app.printers.sdk.host import HostEventBridge, HostPrinterState, adapt_callbacks, state_for_host
from examples.printer_plugins.reference.payload.plugin import ReferencePlugin


def case():
    plugin = ReferencePlugin()
    config = PrinterConfig("", "event-six", "", model="simulator", plugin_id=plugin.id, options={"tool_count": 6})
    callbacks = {name: Mock() for name in ("on_state_change", "on_print_start", "on_print_complete")}
    bridge = HostEventBridge(config, plugin, callbacks)
    connection = plugin.create_connection(config)
    return bridge, callbacks, connection


def job_event(connection, event, sequence=1, state="printing", **changes):
    job = JobProgress(
        job_id="opaque-job-id",
        state=state,
        name="Six tools",
        artifact_uri="host://artifact/1",
        active_tool_ids=[f"tool:{index}" for index in range(6)],
        fraction=1 if state == "completed" else 0,
    )
    connection.state.job = job
    connection.state.job_id = job.job_id
    connection.state.sequence = sequence
    payload = JobEventData(
        job=job,
        artifact=JobArtifact(uri=job.artifact_uri, filename="six-tools.gcode", format="gcode"),
        snapshot=connection.state,
    ).model_dump(mode="json")
    return DeviceEvent(
        device_id=connection.descriptor.device_id, sequence=sequence, event=event, data=payload, **changes
    )


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_six_tool_job_lifecycle_has_host_identity_without_wire_payload(state):
    bridge, callbacks, connection = case()
    assert bridge.emit(job_event(connection, "job.started"))
    assert bridge.emit(job_event(connection, f"job.{state}", sequence=2, state=state))
    start = callbacks["on_print_start"].call_args.args[0]
    end = callbacks["on_print_complete"].call_args.args[0]
    assert (start["subtask_id"], start["filename"]) == ("opaque-job-id", "six-tools.gcode")
    assert end["status"] == state
    assert len(end["sdk_job"]["active_tool_ids"]) == 6
    assert not {"raw_data", "ams_mapping", "hms_errors", "timelapse_was_active"}.intersection(start)
    assert not {"raw_data", "ams_mapping", "hms_errors", "timelapse_was_active"}.intersection(end)
    assert isinstance(callbacks["on_state_change"].call_args.args[0], HostPrinterState)


def test_duplicate_lifecycle_and_older_events_do_not_repeat_callbacks_or_revive_jobs():
    bridge, callbacks, connection = case()
    start = job_event(connection, "job.started")
    assert bridge.emit(start)
    assert not bridge.emit(start)
    assert bridge.emit(job_event(connection, "job.completed", sequence=2, state="completed"))
    assert not bridge.emit(job_event(connection, "job.started", sequence=3))
    assert bridge.latest_snapshot.job.state == "completed"
    callbacks["on_print_start"].assert_called_once()
    callbacks["on_print_complete"].assert_called_once()


def test_identity_resource_and_snapshot_payloads_fail_before_host_callbacks():
    bridge, callbacks, connection = case()
    event = job_event(connection, "job.started")
    with pytest.raises(SDKValidationError, match="another device"):
        bridge.emit(event.model_copy(update={"device_id": "another-printer"}))
    with pytest.raises(SDKValidationError, match="unknown resource"):
        bridge.emit(event.model_copy(update={"resource_id": "heater:missing"}))
    with pytest.raises(ValueError):
        bridge.emit(
            DeviceEvent(device_id=event.device_id, sequence=1, event="resource.telemetry", data={"temperature": 20})
        )
    for callback in callbacks.values():
        callback.assert_not_called()


def test_event_optin_requires_an_explicit_parameter_and_filters_unsupported_callbacks():
    class FixedSignature(ReferencePlugin):
        def create_connection(self, config, *, on_event=None):
            return super().create_connection(config, on_event=on_event)

    bridge, callbacks, _connection = case()
    adapted = adapt_callbacks(bridge.config, FixedSignature(), callbacks)
    assert set(adapted) == {"on_event"}
    assert adapted.event_bridge is not None

    class LegacyCallbacks(ReferencePlugin):
        def create_connection(self, config, **callbacks):
            return super().create_connection(config, **callbacks)

    assert "on_event" not in adapt_callbacks(bridge.config, LegacyCallbacks(), callbacks)


def test_anonymous_snapshot_projects_live_job_without_inventing_an_archive_identity():
    bridge, callbacks, connection = case()
    snapshot = connection.state.model_copy(deep=True)
    snapshot.connected = True
    snapshot.status = "PAUSE"
    snapshot.job = JobProgress(
        state="paused",
        fraction=0.375,
        remaining_seconds=600,
        layer=15,
        total_layers=40,
        active_tool_ids=["tool:5"],
    )
    validate_snapshot(connection.descriptor, snapshot)
    assert bridge.emit(
        DeviceEvent(
            device_id=snapshot.device_id,
            sequence=1,
            event="device.snapshot",
            data=snapshot.model_dump(mode="json"),
        )
    )
    state = callbacks["on_state_change"].call_args.args[0]
    assert state.state == "PAUSE"
    assert state.subtask_id is None and state.current_print is None
    assert state.progress == 37.5 and state.remaining_time == 10
    assert (state.layer_num, state.total_layers) == (15, 40)
    assert state.status_name is None
    callbacks["on_print_start"].assert_not_called()
    callbacks["on_print_complete"].assert_not_called()


@pytest.mark.parametrize("event", ["started", "progress", "completed", "failed", "cancelled"])
@pytest.mark.parametrize("job_id", [None, "   "])
def test_anonymous_job_lifecycle_events_fail_before_any_host_callback(event, job_id):
    bridge, callbacks, connection = case()
    job = JobProgress(job_id=job_id, state=event)
    with pytest.raises(ValidationError, match="stable nonempty job ID"):
        JobEventData(job=job)
    with pytest.raises(ValidationError, match="stable nonempty job ID"):
        bridge.emit(
            DeviceEvent(
                device_id=connection.descriptor.device_id,
                sequence=1,
                event=f"job.{event}",
                data={"job": job.model_dump(mode="json")},
            )
        )
    for callback in callbacks.values():
        callback.assert_not_called()
    assert bridge.latest_snapshot is None


def test_snapshot_job_identity_must_agree_when_the_envelope_declares_an_id():
    _bridge, _callbacks, connection = case()
    snapshot = connection.state.model_copy(deep=True)
    snapshot.job_id = "stable-native-id"
    snapshot.job = JobProgress(state="paused")
    with pytest.raises(SDKValidationError, match="different jobs"):
        validate_snapshot(connection.descriptor, snapshot)
    snapshot.job.job_id = "other-native-id"
    with pytest.raises(SDKValidationError, match="different jobs"):
        validate_snapshot(connection.descriptor, snapshot)
    snapshot.job.job_id = snapshot.job_id
    validate_snapshot(connection.descriptor, snapshot)


@pytest.mark.parametrize(
    "phase, expected", [("PAUSE", None), ("paused", None), ("Waiting for operator", "Waiting for operator")]
)
def test_host_status_name_only_overrides_display_for_a_specific_job_phase(phase, expected):
    bridge, _callbacks, connection = case()
    snapshot = connection.state.model_copy(deep=True)
    snapshot.job = JobProgress(state="paused", phase=phase)
    state = state_for_host(bridge.config, bridge.plugin, snapshot)
    assert state.status_name == expected

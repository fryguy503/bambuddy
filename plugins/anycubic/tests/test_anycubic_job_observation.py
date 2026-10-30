"""Native printer-started jobs remain observations, never adopted dispatches."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import JobProgress, OperationRequest, validate_snapshot
from backend.app.printers.sdk.host import HostEventBridge
from plugins.anycubic.payload import inventory
from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin
from plugins.anycubic.payload.protocol import PREFIX


@pytest.fixture
def observed(monkeypatch):
    plugin = AnycubicPrinterPlugin()
    config = PrinterConfig(
        "192.168.50.50",
        "NATIVE-OBSERVATION",
        "12345678",
        model="Kobra S1",
        plugin_id="anycubic",
        options={"tls_fingerprint": "a" * 64},
    )
    events = []
    callbacks = {
        name: MagicMock() for name in ("on_state_change", "on_print_start", "on_print_complete", "on_print_error")
    }
    bridge = HostEventBridge(config, plugin, callbacks)

    def emit(event):
        events.append(event)
        bridge.emit(event)

    connection = plugin.create_connection(config, on_event=emit)
    native = connection.native
    mqtt = native._client = MagicMock()
    mqtt.publish.return_value.rc = 0
    native._subscribed = True
    native._device_id = "fixture"
    capture = AsyncMock()
    complete = AsyncMock()
    monkeypatch.setattr(inventory, "capture", capture)
    monkeypatch.setattr(inventory, "complete", complete)

    def receive(kind, data, *, action="query"):
        message = SimpleNamespace(
            topic=f"{PREFIX}/printer/public/{native._model_id}/fixture/{kind}/report",
            payload=json.dumps({"type": kind, "action": action, "code": 200, "data": data}).encode(),
        )
        native._received(native._generation, mqtt, message)

    return SimpleNamespace(
        plugin=plugin,
        config=config,
        connection=connection,
        native=native,
        mqtt=mqtt,
        receive=receive,
        events=events,
        callbacks=callbacks,
        bridge=bridge,
        capture=capture,
        complete=complete,
    )


def project(case, phase="paused", **fields):
    case.receive("info", {"state": "busy", "project": {"state": phase, "filename": "printer-started.gcode", **fields}})
    return validate_snapshot(case.plugin.describe_device(case.config), case.connection.state)


@pytest.mark.parametrize(
    "phase,expected", [("printing", "printing"), ("paused", "paused"), ("preheating", "preparing")]
)
def test_native_active_print_is_visible_without_a_dispatch_or_lifecycle_side_effects(observed, phase, expected):
    snapshot = project(
        observed, phase, taskid=-1761750000000, progress=37.5, remain_time=10, curr_layer=75, total_layers=200
    )
    job = snapshot.job
    assert job is not None
    assert job.job_id == snapshot.job_id == "-1761750000000"
    assert job.state == expected
    assert job.name == "printer-started.gcode"
    assert job.fraction == 0.375
    assert (job.remaining_seconds, job.layer, job.total_layers) == (600, 75, 200)
    assert job.elapsed_seconds is None and job.artifact_uri is None
    assert job.extensions["origin"] == "printer" and job.extensions["owned_by_bambuddy"] is False
    host = observed.callbacks["on_state_change"].call_args.args[0]
    assert host.current_print == "printer-started.gcode" and host.progress == 37.5 and host.remaining_time == 10
    observed.receive("print", {"state": "finished", "taskid": -1761750000000, "progress": 100})
    assert observed.connection.state.job.state == "completed"
    assert {event.event for event in observed.events} == {"device.snapshot"}
    for name in ("on_print_start", "on_print_complete", "on_print_error"):
        observed.callbacks[name].assert_not_called()
    observed.capture.assert_not_called()
    observed.complete.assert_not_called()
    assert observed.connection.journal.unresolved() == []
    assert not list((observed.connection.journal.directory / "jobs").glob("*.json"))


@pytest.mark.parametrize("identity", [None, -1, 0])
def test_unreported_native_identity_and_metrics_stay_unknown(observed, identity):
    snapshot = project(observed, taskid=identity)
    assert snapshot.job is not None
    assert snapshot.job_id is None and snapshot.job.job_id is None
    assert snapshot.job.fraction is None and snapshot.job.remaining_seconds is None
    assert snapshot.job.layer is None and snapshot.job.total_layers is None
    assert snapshot.job.extensions["native_job_id"] is None


@pytest.mark.parametrize(
    "native_phase,detail",
    [
        ("paused", None),
        ("printing", None),
        ("resumed", None),
        ("auto_leveling", "Auto leveling"),
        ("flow_calibrating", "Flow calibrating"),
    ],
)
def test_snapshot_phase_only_describes_human_readable_detail(observed, native_phase, detail):
    snapshot = project(observed, native_phase, taskid="native-task")
    assert snapshot.job.phase == detail
    assert observed.native._telemetry.phase == native_phase


@pytest.mark.parametrize(
    "phase,operation,action",
    [("printing", "job.pause", "pause"), ("paused", "job.resume", "resume"), ("paused", "job.stop", "stop")],
)
@pytest.mark.asyncio
async def test_native_job_controls_send_observed_native_id_without_start_or_journal(observed, phase, operation, action):
    project(observed, phase, taskid="native-current-42", progress=12)
    observed.mqtt.publish.reset_mock()
    receipt = await observed.plugin.invoke(
        observed.config, observed.connection, OperationRequest(operation_id=operation)
    )
    assert receipt.status == "accepted"
    envelope = json.loads(observed.mqtt.publish.call_args.args[1])
    assert envelope["action"] == action and envelope["data"] == {"taskid": "native-current-42"}
    assert observed.mqtt.publish.call_count == 1
    assert observed.connection.journal.unresolved() == []


def test_new_native_identity_clears_old_metrics_and_owned_job_projection(observed):
    project(observed, taskid="old-native", progress=80, remain_time=5, curr_layer=80, total_layers=100)
    observed.connection.current_job = JobProgress(
        job_id="sdk-owned-id",
        state="paused",
        fraction=0.8,
        extensions={"native_job_id": "old-native", "native_filename": "printer-started.gcode"},
    )
    assert observed.connection.state.job.job_id == "sdk-owned-id"
    snapshot = project(observed, taskid="different-native")
    assert snapshot.job.job_id == "different-native"
    assert snapshot.job.fraction is None and snapshot.job.remaining_seconds is None
    assert snapshot.job.layer is None and snapshot.job.total_layers is None
    assert snapshot.job.extensions["owned_by_bambuddy"] is False
    observed.receive("info", {"state": "free"})
    assert observed.connection.state.job is None and observed.connection.state.job_id is None


@pytest.mark.parametrize("staleness", ["lifecycle", "transport", "disconnected"])
def test_stale_or_disconnected_native_job_is_not_projected_as_current(observed, staleness):
    project(observed, taskid="native-task", progress=80)
    if staleness == "lifecycle":
        observed.native._last_state_time = time.monotonic() - 61
        observed.receive("tempature", {"curr_nozzle_temp": 210})
    elif staleness == "transport":
        observed.native._last_message_monotonic = time.monotonic() - 61
    else:
        observed.native.state.connected = False
    assert observed.connection.state.job is None


def test_same_filename_without_matching_recorded_native_id_does_not_adopt_external_print(observed):
    observed.connection.journal.begin(
        "held-sdk-job", {"status": "unknown", "native_filename": "printer-started.gcode", "native_job_id": None}
    )
    before = observed.connection.journal.get("held-sdk-job")
    project(observed, taskid="external-native-task", progress=40)
    assert observed.connection.state.job.job_id == "external-native-task"
    assert observed.connection.journal.get("held-sdk-job") == before
    assert {event.event for event in observed.events} == {"device.snapshot"}
    observed.capture.assert_not_called()
    observed.complete.assert_not_called()


def test_owned_job_keeps_sdk_identity_and_updates_progress_from_partial_native_reports(observed):
    observed.connection.journal.begin(
        "owned-sdk-job",
        {
            "status": "accepted",
            "native_filename": "printer-started.gcode",
            "native_job_id": "native-owned-task",
            "uri": "plugin://artifact.gcode",
        },
    )
    project(observed, "printing", taskid="native-owned-task", progress=25, remain_time=10)
    snapshot = observed.connection.state
    assert snapshot.job.job_id == "owned-sdk-job" and snapshot.job.remaining_seconds == 600
    assert snapshot.job.extensions["owned_by_bambuddy"] is True
    observed.callbacks["on_print_start"].assert_called_once()
    observed.receive("print", {"progress": 26, "remain_time": 9})
    assert observed.connection.state.job.job_id == "owned-sdk-job"
    assert observed.connection.state.job.fraction == 0.26
    assert observed.connection.state.job.remaining_seconds == 540
    observed.callbacks["on_print_start"].assert_called_once()


@pytest.mark.parametrize("status,expected", [(0, "idle"), (1, "drying"), (None, "unknown")])
def test_feeder_status_has_provider_normalized_state_and_preserves_raw_telemetry(observed, status, expected):
    drying = {"target_temp": 55, "remain_time": 20}
    if status is not None:
        drying["status"] = status
    observed.receive("info", {"state": "free"})
    observed.receive(
        "multiColorBox",
        {
            "multi_color_box": [
                {"id": 0, "drying_status": drying, "slots": [{"index": index, "status": 4} for index in range(4)]}
            ]
        },
        action="getInfo",
    )
    feeder = next(resource for resource in observed.connection.state.resources if resource.resource_id == "ace:0")
    assert feeder.status == expected
    assert feeder.properties["drying"].value["remaining_minutes"] == 20
    if status is not None:
        observed.native._telemetry.drying_status_observed_at[0] = time.monotonic() - 61
        observed.receive("multiColorBox", {"multi_color_box": [{"id": 0, "drying_status": {"remain_time": 19}}]})
        feeder = next(resource for resource in observed.connection.state.resources if resource.resource_id == "ace:0")
        assert feeder.status == "unknown" and feeder.properties["drying"].quality == "stale"

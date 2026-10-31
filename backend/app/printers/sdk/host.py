"""Project typed resource snapshots into the existing host workflow view.

New code consumes DeviceDescriptor/DeviceSnapshot directly. No packed vendor
slot IDs or proprietary wire payloads are invented by this compatibility view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from inspect import Parameter, signature
from threading import RLock
from typing import Any

from pydantic import model_validator

from backend.app.printers.sdk.domain import DeviceEvent, DeviceSnapshot, JobProgress, OperationError, SDKModel
from backend.app.printers.sdk.jobs import JobArtifact, material_inventory


@dataclass
class HostPrinterState:
    """Core workflow projection of an SDK snapshot, independent of a driver.

    The snapshot remains authoritative for resources and telemetry. Historical
    host readers receive empty accessory values until they consume that API.
    No vendor telemetry classes or protocol payloads are constructed here.
    """

    connected: bool = False
    state: str = "unknown"
    current_print: str | None = None
    subtask_name: str | None = None
    subtask_id: str | None = None
    gcode_file: str | None = None
    progress: float = 0
    remaining_time: int = 0
    layer_num: int = 0
    total_layers: int = 0
    status_name: str | None = None
    plugin_id: str | None = None
    device_snapshot: DeviceSnapshot | None = None
    # A provider's durable dispatch safety gate is independent of the optional
    # host-wide plate confirmation preference.
    plate_clear_required: bool = False
    temperatures: dict[str, float] = field(default_factory=dict)
    material_inventory: list[dict] = field(default_factory=list)
    filament_units: list[dict] = field(default_factory=list)
    external_spools: list[dict] = field(default_factory=list)
    storage: dict = field(default_factory=dict)
    active_plate_id: int | None = None
    dispatched_plate_id: int | None = None
    dispatched_subtask: str | None = None
    firmware_version: str | None = None
    pause_points: list = field(default_factory=list)
    pause_warned_layers: set[int] = field(default_factory=set)
    printable_objects: dict = field(default_factory=dict)
    skipped_objects: list = field(default_factory=list)

    @property
    def _printer_provider(self) -> Any:
        return self.__dict__.get("_provider_owner")

    @_printer_provider.setter
    def _printer_provider(self, provider: Any) -> None:
        self.__dict__["_provider_owner"] = provider

    def __getattr__(self, name: str) -> Any:
        # Compatibility names still read by existing core workflows. They do
        # not describe hardware topology; unsupported accessories stay empty.
        lists = {
            "hms_errors",
            "kprofiles",
            "nozzles",
            "stg",
            "ams_mapping",
            "nozzle_rack",
            "tray_change_log",
            "spool_change_log",
        }
        dictionaries = {"raw_data", "ams_extruder_map", "ams_switch_inlet", "extruder_slots", "h2d_extruder_snow"}
        nulls = {
            "print_options",
            "wifi_signal",
            "developer_mode",
            "ams_filament_backup",
            "fila_switch",
            "print_ams_mapping",
            "cooling_fan_speed",
            "big_fan1_speed",
            "big_fan2_speed",
            "heatbreak_fan_speed",
            "left_aux_fan_speed",
            "current_project_url",
            "last_project_url",
            "nozzle_rack_src_id",
            "nozzle_rack_tar_id",
            "pending_tray_target",
            "pending_tray_target_at",
        }
        defaults = {
            "sdcard": False,
            "sdcard_reported": False,
            "store_to_sdcard": False,
            "timelapse": False,
            "ipcam": False,
            "wired_network": False,
            "door_open": False,
            "chamber_light": False,
            "exhaust_fan_present": False,
            "stg_cur": -1,
            "airduct_mode": 0,
            "speed_level": 2,
            "active_extruder": 0,
            "tray_now": 255,
            "tray_tar": 255,
            "tray_pre": 255,
            "last_loaded_tray": -1,
            "ams_status": 0,
            "ams_status_main": 0,
            "ams_status_sub": 0,
            "mc_print_sub_stage": 0,
            "last_ams_update": 0.0,
        }
        value: Any
        if name in lists:
            value = []
        elif name in dictionaries:
            value = {}
        elif name in nulls:
            value = None
        elif name in defaults:
            value = defaults[name]
        else:
            raise AttributeError(name)
        # Workflow metadata (for example archived pause points) may be changed
        # in place, so each projection owns its compatibility containers.
        setattr(self, name, value)
        return value


def state_for_host(config, plugin, state):
    if not isinstance(state, DeviceSnapshot):
        state.plugin_id = config.plugin_id
        state._printer_provider = plugin
        return state
    from backend.app.printers.sdk.validation import validate_snapshot

    device = plugin.describe_device(config)
    snapshot = validate_snapshot(device, state)
    job = snapshot.job
    phase = job.state if job is not None else snapshot.status
    host_phase = {
        "idle": "IDLE",
        "printing": "RUNNING",
        "running": "RUNNING",
        "paused": "PAUSE",
        "preparing": "PREPARE",
        "slicing": "SLICING",
        "finished": "FINISH",
        "completed": "FINISH",
        "failed": "FAILED",
        "cancelled": "FAILED",
        "unknown": "unknown",
    }.get(phase.casefold(), phase.upper())
    firmware_version = snapshot.extensions.get("firmware_version")
    result = HostPrinterState(
        connected=snapshot.connected,
        state=host_phase,
        current_print=job.name if job else None,
        subtask_name=job.name if job else None,
        subtask_id=job.job_id if job else snapshot.job_id,
        gcode_file=job.artifact_uri if job else None,
        progress=(job.fraction or 0) * 100 if job else 0,
        remaining_time=int((job.remaining_seconds or 0) / 60) if job else 0,
        layer_num=job.layer or 0 if job else 0,
        total_layers=job.total_layers or 0 if job else 0,
        # Only a more specific phase overrides the host's human-readable state.
        # Raw PAUSE/RUNNING labels otherwise bypass its normal status formatter.
        status_name=(
            job.phase
            if job and job.phase and job.phase.casefold() not in {phase.casefold(), host_phase.casefold()}
            else None
        ),
        plugin_id=config.plugin_id,
        firmware_version=firmware_version if isinstance(firmware_version, str) else None,
        device_snapshot=snapshot,
        plate_clear_required=snapshot.extensions.get("plate_clear_required") is True,
    )
    result._printer_provider = plugin
    result.material_inventory = [item.model_dump(mode="json") for item in material_inventory(device, snapshot)]
    descriptions = {resource.id: resource for resource in device.resources}
    for resource in snapshot.resources:
        descriptor = descriptions[resource.resource_id]
        for name in ("temperature", "target"):
            value = resource.properties.get(name)
            if value is None or value.quality != "known" or not isinstance(value.value, (int, float)):
                continue
            key = descriptor.id if name == "temperature" else f"{descriptor.id}_target"
            result.temperatures[key] = float(value.value)
            for role in ("bed", "chamber"):
                if descriptor.kind == role or role in descriptor.roles:
                    result.temperatures[role if name == "temperature" else f"{role}_target"] = float(value.value)
    return result


class JobEventData(SDKModel):
    """Payload for a device's explicit job lifecycle events."""

    job: JobProgress
    artifact: JobArtifact | None = None
    snapshot: DeviceSnapshot | None = None
    error: OperationError | None = None

    @model_validator(mode="after")
    def _require_job_identity(self):
        if self.job.job_id is None or not self.job.job_id.strip():
            raise ValueError("Job lifecycle events require a stable nonempty job ID")
        return self


class HostEventBridge:
    """Connection-local typed events, projected onto existing host workflows.

    A plugin owns its event sequence and job identities. Duplicate/older events
    and repeated lifecycle notifications for a job are ignored. The callback is
    synchronous and can be called from protocol threads, like the host's other
    connection callbacks; the manager schedules asynchronous host work.
    """

    def __init__(self, config, plugin, callbacks):
        self.config = config
        self.plugin = plugin
        self.callbacks = callbacks
        self.latest_snapshot: DeviceSnapshot | None = None
        self._sequence = -1
        self._started_jobs: dict[str, None] = {}
        self._terminal_jobs: dict[str, None] = {}
        self._lock = RLock()

    def _remember(self, collection, job_id):
        collection[job_id] = None
        if len(collection) > 256:
            collection.pop(next(iter(collection)))

    def adopt_lifecycle(self, previous: HostEventBridge, *, exclude_job_ids: set[str] | None = None) -> None:
        """Retain completed/start deduplication across an observer refresh.

        Sequences and resource snapshots belong to each connection generation
        and deliberately are not copied to the new provider. An outgoing
        observer may have seen a lifecycle event whose host callback belongs
        to another retained observer; those observations must not suppress the
        original owner's eventual callback when it is revived by rollback.
        """
        excluded = exclude_job_ids or set()
        with previous._lock:
            started = {key: value for key, value in previous._started_jobs.items() if key not in excluded}
            terminal = {key: value for key, value in previous._terminal_jobs.items() if key not in excluded}
        with self._lock:
            self._started_jobs.update(started)
            self._terminal_jobs.update(terminal)

    def _notify(self, name, value):
        callback = self.callbacks.get(name)
        if callback is not None:
            callback(value)

    def emit(self, event: DeviceEvent | dict) -> bool:
        from backend.app.printers.sdk.validation import SDKValidationError, validate_snapshot

        event = event if isinstance(event, DeviceEvent) else DeviceEvent.model_validate(event)
        descriptor = self.plugin.describe_device(self.config)
        if event.device_id != descriptor.device_id:
            raise SDKValidationError("Event belongs to another device")
        if event.observed_at.tzinfo is None:
            raise SDKValidationError("Event timestamps require an explicit timezone")
        if event.resource_id is not None and event.resource_id not in {item.id for item in descriptor.resources}:
            raise SDKValidationError("Event references an unknown resource")
        with self._lock:
            if event.sequence <= self._sequence:
                return False
            job_data = None
            snapshot = None
            if event.event in {"device.snapshot", "device.status", "resource.telemetry"}:
                snapshot = validate_snapshot(descriptor, DeviceSnapshot.model_validate(event.data))
            elif event.event in {"job.started", "job.progress", "job.completed", "job.failed", "job.cancelled"}:
                job_data = JobEventData.model_validate(event.data)
                # Retain a runtime guard even for previously constructed or
                # copied Pydantic instances, which may bypass revalidation.
                job_id = job_data.job.job_id
                if job_id is None or not job_id.strip():
                    raise SDKValidationError("Job lifecycle events require a stable nonempty job ID")
                tools = {item.id for item in descriptor.resources if item.kind == "tool"}
                if not set(job_data.job.active_tool_ids).issubset(tools):
                    raise SDKValidationError("Job event references an unknown tool")
                expected = {
                    "job.completed": {"completed", "finished"},
                    "job.failed": {"failed"},
                    "job.cancelled": {"cancelled"},
                }
                if event.event in expected and job_data.job.state.casefold() not in expected[event.event]:
                    raise SDKValidationError("Job state does not match the lifecycle event")
                if job_data.snapshot is not None:
                    snapshot = validate_snapshot(descriptor, job_data.snapshot)
                    if (
                        snapshot.job_id not in {None, job_data.job.job_id}
                        or snapshot.job
                        and snapshot.job.job_id != job_data.job.job_id
                    ):
                        raise SDKValidationError("Snapshot and lifecycle event identify different jobs")
                else:
                    # A job event can advance the host job view without
                    # inventing telemetry for any physical resource.
                    base = self.latest_snapshot or DeviceSnapshot(
                        device_id=descriptor.device_id, descriptor_revision=descriptor.revision
                    )
                    snapshot = base.model_copy(
                        update={
                            "job": job_data.job,
                            "job_id": job_data.job.job_id,
                            "status": job_data.job.state,
                            "observed_at": event.observed_at,
                        }
                    )
            else:
                # Custom events remain available in the SDK without acquiring
                # implicit semantics in historical print/archive workflows.
                return False
            if job_data is not None:
                job_id = job_data.job.job_id
                repeated_start = event.event == "job.started" and job_id in self._started_jobs
                if repeated_start or job_id in self._terminal_jobs:
                    self._sequence = event.sequence
                    return False
            if (
                snapshot is not None
                and self.latest_snapshot is not None
                and snapshot.sequence < self.latest_snapshot.sequence
            ):
                raise SDKValidationError("Event snapshot sequence moved backwards")
            self._sequence = event.sequence
            if snapshot is not None:
                self.latest_snapshot = snapshot
                self._notify("on_state_change", state_for_host(self.config, self.plugin, snapshot))
            if job_data is None:
                return True
            job = job_data.job
            if event.event == "job.progress":
                if job.fraction is not None:
                    self._notify("on_print_progress", round(job.fraction * 100))
                if job.layer is not None:
                    self._notify("on_layer_change", job.layer)
                return True
            filename = (
                (job_data.artifact.filename if job_data.artifact else None)
                or job.artifact_uri
                or job.name
                or job.job_id
            )
            payload = {
                "plugin_id": self.config.plugin_id,
                "job_id": job.job_id,
                "subtask_id": job.job_id,
                "filename": filename,
                "subtask_name": job.name or filename,
                "status": event.event.removeprefix("job.") if event.event != "job.started" else "printing",
                "remaining_time": job.remaining_seconds,
                "elapsed_seconds": job.elapsed_seconds,
                "last_progress": job.fraction * 100 if job.fraction is not None else None,
                "last_layer_num": job.layer,
                "sdk_job": job.model_dump(mode="json", exclude_none=True),
                "sdk_event": event.model_dump(mode="json", exclude_none=True),
            }
            if job_data.artifact is not None:
                payload["artifact"] = job_data.artifact.model_dump(mode="json", exclude_none=True)
            if job_data.error is not None:
                payload["error"] = job_data.error.model_dump(mode="json")
                if event.event == "job.failed":
                    payload["failure_reason"] = job_data.error.code
                    payload["error_message"] = job_data.error.message
            if event.event == "job.started":
                if job.job_id in self._started_jobs or job.job_id in self._terminal_jobs:
                    return False
                self._remember(self._started_jobs, job.job_id)
                self._notify("on_print_start", payload)
            else:
                if job.job_id in self._terminal_jobs:
                    return False
                self._remember(self._terminal_jobs, job.job_id)
                self._notify("on_print_complete", payload)
            return True


class AdaptedCallbacks(dict):
    event_bridge: HostEventBridge | None = None


def adapt_callbacks(config, plugin, callbacks):
    """Project snapshots and offer typed events only to explicitly opted-in plugins."""
    result = AdaptedCallbacks(callbacks)
    callback = result.get("on_state_change")
    if callback is not None:

        def on_state_change(state):
            return callback(state_for_host(config, plugin, state))

        result["on_state_change"] = on_state_change
    if plugin.api_version == 2:
        parameters = signature(plugin.create_connection).parameters
        event_parameter = parameters.get("on_event")
        if event_parameter is not None and event_parameter.kind in {
            Parameter.KEYWORD_ONLY,
            Parameter.POSITIONAL_OR_KEYWORD,
        }:
            result.event_bridge = HostEventBridge(config, plugin, callbacks)
            result["on_event"] = result.event_bridge.emit
        if not any(parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()):
            for name in list(result):
                if name not in parameters:
                    result.pop(name)
    return result

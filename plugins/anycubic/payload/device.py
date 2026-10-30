"""Typed topology and telemetry for signed Anycubic LAN models."""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from backend.app.printers.sdk import (
    DeviceDescriptor,
    DeviceSnapshot,
    JobProgress,
    JobRequest,
    OperationDescriptor,
    ResourceDescriptor,
    ResourceProperty,
    ResourceState,
    TelemetryValue,
)

from .models import get_model
from .protocol import AnycubicError, task_id


def device_id(config):
    return "anycubic:" + hashlib.sha256(config.serial_number.encode()).hexdigest()[:24]


def source_id(box, slot):
    return f"material:ace:{box}:{slot}"


def native_slot(resource_id):
    parts = resource_id.split(":")
    if len(parts) != 4 or parts[:2] != ["material", "ace"]:
        raise AnycubicError("Select an ACE material resource")
    return int(parts[2]), int(parts[3])


def prop(label, schema=None, unit=None, operation=None, parameter=None, writable=False):
    return ResourceProperty(
        label=label,
        value_schema=schema or {},
        unit=unit,
        writable=writable,
        operation_id=operation,
        parameter=parameter,
    )


def operation(
    id,
    label,
    properties=None,
    required=(),
    kinds=(),
    *,
    permission="printers:control",
    asynchronous=True,
    confirmation=None,
):
    return OperationDescriptor(
        id=id,
        label=label,
        resource_kinds=list(kinds),
        parameters_schema={
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
        result_schema={},
        permission=permission,
        asynchronous=asynchronous,
        timeout_seconds=60,
        confirmation=confirmation,
        extensions={"delivery": "queued commands await observed native telemetry"} if asynchronous else {},
    )


def describe(plugin, config, native=None):
    model = get_model(config.model)
    if model is None:
        raise AnycubicError("Select a supported Anycubic model")
    caps = native.control_capabilities() if native is not None else {}
    ready = bool(caps.get("ready"))
    resources = [
        ResourceDescriptor(
            id="tool:primary",
            kind="tool",
            label="Primary tool",
            relations={"hotends": ["hotend:primary"], "extruders": ["extruder:primary"]},
        ),
        ResourceDescriptor(
            id="hotend:primary",
            kind="hotend",
            label="Hotend",
            parent_id="tool:primary",
            relations={"heaters": ["heater:nozzle"]},
        ),
        ResourceDescriptor(id="extruder:primary", kind="extruder", label="Extruder", parent_id="tool:primary"),
        ResourceDescriptor(
            id="heater:nozzle",
            kind="heater",
            label="Nozzle",
            parent_id="hotend:primary",
            roles=["hotend"],
            properties={
                "temperature": prop("Temperature", {"type": "number"}, "degC"),
                "target": prop(
                    "Target",
                    {"type": "integer", "minimum": 0, "maximum": caps.get("nozzle_max", model.nozzle_max)},
                    "degC",
                    "heater.set_target",
                    "target",
                    ready,
                ),
            },
        ),
        ResourceDescriptor(
            id="heater:bed",
            kind="heater",
            label="Bed",
            roles=["bed"],
            properties={
                "temperature": prop("Temperature", {"type": "number"}, "degC"),
                "target": prop(
                    "Target",
                    {"type": "integer", "minimum": 0, "maximum": caps.get("bed_max", model.bed_max)},
                    "degC",
                    "heater.set_target",
                    "target",
                    ready,
                ),
            },
        ),
        ResourceDescriptor(id="build_surface:primary", kind="build_surface", label="Build plate"),
        ResourceDescriptor(
            id="sensor:chamber",
            kind="sensor",
            label="Chamber temperature",
            roles=["chamber"],
            properties={"temperature": prop("Temperature", {"type": "number"}, "degC")},
        ),
        ResourceDescriptor(id="camera:primary", kind="camera", label="LAN camera", capabilities=["mjpeg", "snapshot"]),
        ResourceDescriptor(id="storage:local", kind="storage", label="Printer files", capabilities=["list", "upload"]),
    ]
    for axis in "xyz":
        resources.append(ResourceDescriptor(id=f"axis:{axis}", kind="axis", label=axis.upper()))
    for fan in caps.get("reported_fans", []) if native is not None else ("part", "aux", "chamber"):
        resources.append(
            ResourceDescriptor(
                id=f"fan:{fan}",
                kind="fan",
                label=fan.title() + " fan",
                properties={
                    "percent": prop(
                        "Speed",
                        {"type": "integer", "minimum": 0, "maximum": 100},
                        "%",
                        "fan.set_percent",
                        "percent",
                        ready and fan in caps.get("fans", []),
                    )
                },
            )
        )
    resources.append(
        ResourceDescriptor(
            id="light:chamber",
            kind="light",
            label="Chamber light",
            properties={
                "on": prop(
                    "On",
                    {"type": "boolean"},
                    operation="light.set",
                    parameter="on",
                    writable=bool(caps.get("chamber_light")),
                )
            },
        )
    )
    resources.append(
        ResourceDescriptor(
            id="settings:printer",
            kind="settings",
            label="Printer settings",
            properties={
                "speed_mode": prop(
                    "Speed mode",
                    {"type": "integer", "enum": [1, 2, 3]},
                    operation="anycubic.settings.speed",
                    parameter="mode",
                    writable=ready,
                )
            },
        )
    )
    resources.append(
        ResourceDescriptor(
            id="material:external",
            kind="material_source",
            label="External spool",
            relations={"tools": ["tool:primary"]},
            properties={"spool_id": prop("Assigned spool", {"type": ["string", "null"]})},
        )
    )
    for box in model.ace_ids:
        feeder = f"ace:{box}"
        resources.append(
            ResourceDescriptor(
                id=feeder,
                kind="feeder",
                label=(getattr(native, "_display_names", {}).get(feeder) if native is not None else None)
                or (f"ACE {box}" if box >= 0 else "Internal feeder"),
                metadata={"native_box_id": box},
                properties={
                    "temperature": prop("Temperature", {"type": "number"}, "degC"),
                    "humidity": prop("Humidity", {"type": "number"}, "%"),
                    "drying": prop("Drying", {"type": "object"}),
                    "name": prop("Friendly name", {"type": "string"}),
                },
            )
        )
        for slot in range(4):
            resources.append(
                ResourceDescriptor(
                    id=source_id(box, slot),
                    kind="material_source",
                    label=f"ACE {box} slot {slot + 1}",
                    parent_id=feeder,
                    relations={"tools": ["tool:primary"]},
                    metadata={"native_box_id": box, "native_slot_id": slot},
                    properties={
                        "material_type": prop("Material", {"type": "string"}),
                        "color": prop("Color", {"type": "string"}),
                        "remaining_percent": prop("Remaining", {"type": "number", "minimum": 0, "maximum": 100}, "%"),
                        "spool_id": prop("Assigned spool", {"type": ["string", "integer", "null"]}),
                        "editable": prop("Editable", {"type": "boolean"}),
                    },
                )
            )
    operations = [
        operation(
            "heater.set_target",
            "Set temperature",
            {"target": {"type": "integer", "minimum": 0, "maximum": model.nozzle_max}},
            ["target"],
            ["heater"],
        ),
        operation(
            "fan.set_percent",
            "Set fan",
            {"percent": {"type": "integer", "minimum": 0, "maximum": 100}},
            ["percent"],
            ["fan"],
        ),
        operation("light.set", "Set light", {"on": {"type": "boolean"}}, ["on"], ["light"]),
        operation("job.pause", "Pause print"),
        operation("job.resume", "Resume print"),
        operation("job.stop", "Stop print", confirmation="Stop the active print?"),
        OperationDescriptor(
            id="job.start",
            label="Start validated sliced job",
            parameters_schema=JobRequest.model_json_schema(),
            asynchronous=True,
            timeout_seconds=60,
            additional_permissions=["printers:files"],
            confirmation="Start this job on the printer?",
        ),
        operation("telemetry.refresh", "Refresh status", asynchronous=False),
        operation(
            "anycubic.settings.speed",
            "Set speed mode",
            {"mode": {"type": "integer", "enum": [1, 2, 3]}},
            ["mode"],
            ["settings"],
        ),
        operation(
            "anycubic.motion.jog",
            "Jog axis",
            {"distance": {"type": "number", "minimum": -50, "maximum": 50}},
            ["distance"],
            ["axis"],
        ),
        operation(
            "anycubic.motion.home", "Home axes", {"axes": {"type": "string", "enum": ["all", "xy", "z"]}}, ["axes"]
        ),
        operation(
            "anycubic.extruder.pulse",
            "Extrude/retract briefly",
            {"direction": {"type": "string", "enum": ["extrude", "retract"]}},
            ["direction"],
            ["extruder"],
        ),
        operation("anycubic.extruder.stop", "Stop extrusion", kinds=["extruder"]),
        operation(
            "anycubic.material.configure",
            "Configure material",
            {
                "material": {"type": "string", "minLength": 1, "maxLength": 64},
                "color": {"type": "string", "pattern": "^#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$"},
            },
            ["material", "color"],
            ["material_source"],
        ),
        operation(
            "anycubic.material.feed",
            "Load/unload material",
            {"action": {"type": "string", "enum": ["load", "unload", "stop"]}},
            ["action"],
            ["material_source"],
        ),
        operation("anycubic.material.refresh", "Refresh material", kinds=["material_source"]),
        operation(
            "anycubic.drying.set",
            "Start/apply drying",
            {
                "temperature": {"type": "integer", "minimum": 40, "maximum": 65},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
            },
            ["temperature", "duration_minutes"],
            ["feeder"],
        ),
        operation("anycubic.drying.stop", "Stop drying", kinds=["feeder"]),
        operation(
            "job.clear_plate",
            "Confirm plate cleared",
            permission="printers:clear_plate",
            asynchronous=False,
            confirmation="Confirm the printer is idle and the build plate is clear.",
        ),
        operation(
            "anycubic.job.resolve",
            "Confirm uncertain job is not printing",
            {"job_id": {"type": "string"}},
            ["job_id"],
            asynchronous=False,
            confirmation="Verify the printer is idle before resolving the uncertain dispatch.",
        ),
        operation(
            "anycubic.discovery.inspect",
            "Inspect identity and public TLS fingerprint",
            {"ip_address": {"type": "string"}},
            ["ip_address"],
            permission="discovery:scan",
            asynchronous=False,
        ),
        operation(
            "anycubic.files.list",
            "List native files",
            {
                "path": {"type": "string"},
                "page": {"type": "integer", "minimum": 1, "maximum": 10000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "keyword": {"type": "string", "maxLength": 100},
            },
            permission="printers:files",
            asynchronous=False,
        ),
        operation(
            "anycubic.inventory.assign",
            "Assign physical spool",
            {"spool_id": {"type": ["integer", "null"], "minimum": 1}},
            ["spool_id"],
            ["material_source"],
            permission="inventory:update",
            asynchronous=False,
        ),
        operation(
            "anycubic.ace.history",
            "ACE sensor history",
            {"hours": {"type": "integer", "minimum": 1, "maximum": 336}},
            kinds=["feeder"],
            permission="printers:read",
            asynchronous=False,
        ),
        operation(
            "anycubic.ace.rename",
            "Name ACE unit",
            {"name": {"type": "string", "maxLength": 100}},
            ["name"],
            ["feeder"],
            permission="printers:update",
            asynchronous=False,
        ),
    ]
    operation_by_id = {item.id: item for item in operations}
    operation_by_id["heater.set_target"].resource_parameters_schemas = {
        "heater:nozzle": {
            "type": "object",
            "properties": {
                "target": {"type": "integer", "minimum": 0, "maximum": caps.get("nozzle_max", model.nozzle_max)}
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        "heater:bed": {
            "type": "object",
            "properties": {"target": {"type": "integer", "minimum": 0, "maximum": caps.get("bed_max", model.bed_max)}},
            "required": ["target"],
            "additionalProperties": False,
        },
    }
    revision = hashlib.sha256(
        json.dumps({"resources": [r.model_dump(mode="json") for r in resources], "caps": caps}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return DeviceDescriptor(
        device_id=device_id(config),
        model_id=model.name,
        name=f"Anycubic {model.name}",
        revision=revision,
        resources=resources,
        operations=operations,
        configuration_schema=plugin.effective_configuration_schema(),
        capabilities=sorted(plugin.capabilities),
        extensions={"native_model_id": model.model_id, "artifact_formats": ["gcode"], "features": caps},
    )


def observed_job(native, owned: JobProgress | None = None) -> JobProgress | None:
    """Project current native telemetry without acquiring ownership of a print.

    A printer/slicer-started print has no Bambuddy dispatch identity. It is a
    snapshot observation only: no journal, lifecycle callback or inventory
    accounting is created by this function.
    """
    state = native.state
    if (
        not state.connected
        or native.is_stale
        or not native._last_state_time
        or not 0 <= time.monotonic() - native._last_state_time <= native.STALE_TIMEOUT
    ):
        return None
    phase = {
        "RUNNING": "printing",
        "PAUSE": "paused",
        "PREPARE": "preparing",
        "FINISH": "completed",
        "FAILED": "cancelled" if native._telemetry.phase in {"stoped", "stopped"} else "failed",
    }.get(state.state)
    if phase is None:
        return None
    identity = task_id(state.subtask_id)
    filename = PurePosixPath((state.gcode_file or "").replace("\\", "/")).name or None
    metrics = state.job_metrics
    native_phase = native._telemetry.phase
    detail = (
        native_phase.replace("_", " ").capitalize()
        if native_phase
        in {
            "preheating",
            "auto_leveling",
            "vibrating",
            "flow_calibrating",
            "downloading",
            "checking",
            "pausing",
            "resuming",
            "stopping",
        }
        else None
    )
    observation = JobProgress(
        job_id=identity,
        state=phase,
        phase=detail,
        name=state.current_print or filename,
        fraction=max(0, min(1, state.progress / 100)) if "progress" in metrics else None,
        remaining_seconds=max(0, state.remaining_time) * 60 if "remaining_time" in metrics else None,
        layer=max(0, state.layer_num) if "layer_num" in metrics else None,
        total_layers=max(0, state.total_layers) if "total_layers" in metrics else None,
        active_tool_ids=["tool:primary"],
        extensions={
            "origin": "printer",
            "owned_by_bambuddy": False,
            "native_job_id": identity,
            "native_filename": filename,
        },
    )
    expected = task_id(owned.extensions.get("native_job_id")) if owned else None
    expected_filename = owned.extensions.get("native_filename") if owned else None
    if owned is not None and (
        expected is not None
        and identity == expected
        and (not expected_filename or not filename or expected_filename == filename)
    ):
        return owned.model_copy(
            update={
                "state": observation.state,
                "phase": observation.phase,
                "fraction": observation.fraction,
                "remaining_seconds": observation.remaining_seconds,
                "layer": observation.layer,
                "total_layers": observation.total_layers,
                "extensions": {**owned.extensions, "origin": "bambuddy", "owned_by_bambuddy": True},
            }
        )
    return observation


def snapshot(plugin, config, native, sequence=0, job=None, links=None, names=None) -> DeviceSnapshot:
    links = links or {}
    names = names or {}
    descriptor = describe(plugin, config, native)
    state = native.state
    now = datetime.now(timezone.utc)
    stale = not state.connected or native.is_stale
    job = observed_job(native, job)
    quality = "stale" if stale else "known"
    values = []
    for resource in descriptor.resources:
        data = {}
        available = state.connected
        resource_status = None
        drying_fresh = False
        if resource.id.startswith("heater:"):
            kind = "nozzle" if resource.id.endswith("nozzle") else "bed"
            data = {"temperature": state.temperatures.get(kind), "target": state.temperatures.get(kind + "_target")}
        elif resource.id == "sensor:chamber":
            data = {"temperature": state.temperatures.get("chamber")}
        elif resource.id == "light:chamber":
            data = {"on": state.chamber_light}
        elif resource.id == "settings:printer":
            data = {"speed_mode": state.speed_level}
        elif resource.kind == "fan":
            fan = resource.id.split(":")[-1]
            key = {"part": "cooling_fan_speed", "aux": "big_fan1_speed", "chamber": "big_fan2_speed"}[fan]
            data = {"percent": getattr(state, key, None)}
        elif resource.kind == "feeder":
            box_id = resource.metadata["native_box_id"]
            box = native._telemetry.boxes.get(box_id)
            available = bool(state.connected and box is not None)
            resource_status = "unknown"
            if box is not None:
                data = {
                    "temperature": box.get("temp"),
                    "humidity": box.get("humidity_raw"),
                    "drying": box.get("anycubic_drying"),
                }
                observed = native._telemetry.drying_status_observed_at.get(box_id)
                drying_fresh = (
                    not stale and observed is not None and 0 <= time.monotonic() - observed <= native.STALE_TIMEOUT
                )
                if drying_fresh:
                    resource_status = {0: "idle", 1: "drying"}.get(
                        box.get("anycubic_drying", {}).get("status"), "unknown"
                    )
        elif resource.kind == "material_source" and resource.id != "material:external":
            box_id, slot = native_slot(resource.id)
            box = native._telemetry.boxes.get(box_id, {})
            tray = next((t for t in box.get("tray", []) if t.get("id") == slot), None)
            available = bool(state.connected and tray and tray.get("tray_type"))
            if tray:
                data = {
                    "material_type": tray.get("tray_type"),
                    "color": ("#" + tray["tray_color"][:6]) if tray.get("tray_color") else None,
                    "remaining_percent": tray.get("remain"),
                    "editable": tray.get("anycubic_edit_status") in (1, 3),
                }
        if resource.id in links:
            data["spool_id"] = str(links[resource.id]["spool_id"])
        if resource.kind == "feeder" and resource.id in names:
            data["name"] = names[resource.id]
        properties = {
            key: TelemetryValue(
                value=value,
                quality=("stale" if key == "drying" and not drying_fresh else quality)
                if value is not None
                else "unknown",
                unit=resource.properties[key].unit,
                observed_at=now,
                valid_until=now + timedelta(seconds=60),
            )
            for key, value in data.items()
            if key in resource.properties
        }
        values.append(
            ResourceState(resource_id=resource.id, available=available, status=resource_status, properties=properties)
        )
    return DeviceSnapshot(
        device_id=descriptor.device_id,
        descriptor_revision=descriptor.revision,
        sequence=sequence,
        connected=state.connected,
        status=state.state,
        job_id=job.job_id if job else None,
        job=job,
        resources=values,
        extensions={"firmware_version": state.firmware_version},
    )

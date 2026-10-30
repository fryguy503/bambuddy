"""Bambu's hardware topology, limits and translations for the generic SDK.

This module is part of the independently replaceable plugin payload. Core
never assigns meaning to the protocol's nozzle, fan or material slot numbers.
"""

import hashlib
import json
from inspect import Parameter, signature
from typing import Any, get_type_hints

from backend.app.printers.sdk import (
    DeviceDescriptor,
    DeviceSnapshot,
    OperationDescriptor,
    ResourceDescriptor,
    ResourceProperty,
    ResourceState,
    TelemetryValue,
)

from . import capabilities, models


def _object(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def _property(label, schema, unit=None, operation=None, parameter=None):
    return ResourceProperty(
        label=label,
        value_schema=schema,
        unit=unit,
        writable=operation is not None,
        operation_id=operation,
        parameter=parameter,
    )


def _job_schema():
    from backend.app.printers.sdk import JobRequest

    schema = JobRequest.model_json_schema()
    schema["properties"]["plate_id"] = {
        "anyOf": [{"type": "string", "pattern": r"^[1-9][0-9]{0,2}$"}, {"type": "null"}]
    }
    schema["properties"]["options"] = _object(
        {
            **{name: {"enum": ["off", "on", "auto"]} for name in ("bed_levelling", "flow_cali", "nozzle_offset_cali")},
            **{
                name: {"type": "boolean"}
                for name in ("vibration_cali", "layer_inspect", "timelapse", "use_ams", "plate_uses_only_external")
            },
            **{
                name: {"type": ["string", "null"], "maxLength": 32768}
                for name in ("nozzle_mapping", "nozzle_slot_extruders")
            },
        }
    )
    # Bambu indexes sliced filament inputs from one. Bound this vendor array
    # before allocating it, while leaving SDK input IDs opaque for other drivers.
    schema["$defs"]["MaterialBinding"]["properties"]["input_id"] = {
        "type": "string",
        "pattern": r"^(?:[1-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-6])$",
    }
    return schema


def describe(config, version="1.0.0", state=None) -> DeviceDescriptor:
    dual = models.is_dual_nozzle_model(config.model)
    count = 2 if dual else 1
    if state is not None and "nozzle_2" in state.temperatures:
        count = max(count, len(state.nozzles))
        dual = count > 1
    resources = []
    heater_limits = {}
    for index in range(count):
        tool, hotend, heater, extruder = (f"{kind}:{index}" for kind in ("tool", "hotend", "heater", "extruder"))
        resources.extend(
            [
                ResourceDescriptor(
                    id=tool,
                    kind="tool",
                    label=("Right tool" if index == 0 else "Left tool") if dual else "Tool",
                    relations={"hotends": [hotend], "extruders": [extruder]},
                    properties={"selected": _property("Selected", {"type": "boolean"})},
                ),
                ResourceDescriptor(
                    id=hotend,
                    kind="hotend",
                    label=f"Hotend {index + 1}",
                    parent_id=tool,
                    relations={"heaters": [heater]},
                ),
                ResourceDescriptor(id=extruder, kind="extruder", label=f"Extruder {index + 1}", parent_id=tool),
            ]
        )
        heater_limits[heater] = 320
    heater_limits["heater:bed"] = 140
    resources.append(
        ResourceDescriptor(id="bed", kind="bed", label="Build plate", relations={"heaters": ["heater:bed"]})
    )
    if capabilities.supports_chamber_heater(config.model):
        heater_limits["heater:chamber"] = 65
        resources.append(
            ResourceDescriptor(id="chamber", kind="chamber", label="Chamber", relations={"heaters": ["heater:chamber"]})
        )
    for heater, maximum in heater_limits.items():
        label = (
            "Bed"
            if heater == "heater:bed"
            else "Chamber"
            if heater == "heater:chamber"
            else f"Hotend {int(heater.split(':')[-1]) + 1}"
        )
        resources.append(
            ResourceDescriptor(
                id=heater,
                kind="heater",
                label=label,
                properties={
                    "current": _property("Current temperature", {"type": "number"}, "degC"),
                    "target": _property(
                        "Target temperature",
                        {"type": "integer", "minimum": 0, "maximum": maximum},
                        "degC",
                        "heater.set_target",
                        "target",
                    ),
                },
            )
        )
    fans = ["part"]
    if config.model not in {"A1", "A1 Mini", "A2L"}:
        fans += ["aux", "chamber"]
    if models.normalize_printer_model(config.model) in {"P2S", "X2D"}:
        fans.append("aux2")
    for fan in fans:
        resources.append(
            ResourceDescriptor(
                id=f"fan:{fan}",
                kind="fan",
                label=f"{fan.title()} fan",
                properties={
                    "speed": _property(
                        "Speed", {"type": "integer", "minimum": 0, "maximum": 100}, "%", "fan.set_speed", "speed"
                    ),
                },
            )
        )
    for axis in ("x", "y", "z"):
        resources.append(
            ResourceDescriptor(
                id=f"axis:{axis}",
                kind="axis",
                label=axis.upper(),
                properties={"position": _property("Position", {"type": "number"}, "mm")},
            )
        )
    resources.extend(
        [
            ResourceDescriptor(
                id="light:chamber",
                kind="light",
                label="Chamber light",
                properties={"on": _property("On", {"type": "boolean"}, operation="light.set", parameter="on")},
            ),
            ResourceDescriptor(id="camera:main", kind="camera", label="Printer camera"),
            ResourceDescriptor(id="storage:prints", kind="storage", label="Printer files"),
        ]
    )
    if state is not None:
        raw = state.raw_data if isinstance(state.raw_data, dict) else {}
        for unit in raw.get("ams", []) if isinstance(raw.get("ams", []), list) else []:
            unit_id = int(unit.get("id", 0))
            feeder = f"feeder:ams:{unit_id}"
            resources.append(ResourceDescriptor(id=feeder, kind="feeder", label=f"AMS {unit_id}"))
            for tray in unit.get("tray", []):
                slot_id = int(tray.get("id", 0))
                resources.append(
                    ResourceDescriptor(
                        id=f"material:ams:{unit_id}:{slot_id}",
                        kind="material_source",
                        label=f"AMS {unit_id} slot {slot_id + 1}",
                        parent_id=feeder,
                        relations={"tools": [f"tool:{index}" for index in range(count)]},
                        properties={
                            "material": _property("Material", {"type": "string"}),
                            "color": _property("Color", {"type": "string"}),
                            "remaining": _property("Remaining", {"type": "number", "minimum": 0}, "%"),
                        },
                    )
                )
    operations = [
        OperationDescriptor(
            id="heater.set_target",
            label="Set temperature",
            resource_kinds=["heater"],
            parameters_schema=_object({"target": {"type": "integer", "minimum": 0}}, ["target"]),
            resource_parameters_schemas={
                rid: _object({"target": {"type": "integer", "minimum": 0, "maximum": maximum}}, ["target"])
                for rid, maximum in heater_limits.items()
            },
            result_schema={"type": "boolean"},
            idempotent=True,
        ),
        OperationDescriptor(
            id="tool.select", label="Select tool", resource_kinds=["tool"], result_schema={"type": "boolean"}
        ),
        OperationDescriptor(
            id="fan.set_speed",
            label="Set fan speed",
            resource_kinds=["fan"],
            parameters_schema=_object({"speed": {"type": "integer", "minimum": 0, "maximum": 100}}, ["speed"]),
            result_schema={"type": "boolean"},
            idempotent=True,
        ),
        OperationDescriptor(
            id="light.set",
            label="Set light",
            resource_kinds=["light"],
            parameters_schema=_object({"on": {"type": "boolean"}}, ["on"]),
            result_schema={"type": "boolean"},
            idempotent=True,
        ),
        OperationDescriptor(
            id="motion.jog",
            label="Move axis",
            resource_kinds=["axis"],
            parameters_schema=_object(
                {
                    "distance": {"type": "number", "minimum": -50, "maximum": 50},
                    "speed": {"type": "number", "minimum": 1, "maximum": 3000, "default": 600},
                },
                ["distance"],
            ),
            result_schema={"type": "boolean"},
        ),
        OperationDescriptor(
            id="motion.home",
            label="Home axes",
            parameters_schema=_object(
                {"axes": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": ["X", "Y", "Z"]}}}
            ),
            result_schema={"type": "boolean"},
        ),
        OperationDescriptor(
            id="job.speed",
            label="Print speed",
            parameters_schema=_object({"mode": {"enum": [1, 2, 3, 4], "default": 2}}, ["mode"]),
            result_schema={"type": "boolean"},
        ),
    ]
    operations.append(
        OperationDescriptor(
            id="job.start",
            label="Start printing job",
            parameters_schema=_job_schema(),
            permission="printers:files",
            additional_permissions=["queue:create"],
            result_schema={"type": "boolean"},
            extensions={"host_workflow": "queue"},
        )
    )
    if any(resource.kind == "material_source" for resource in resources):
        operations.extend(
            [
                OperationDescriptor(
                    id="material.load",
                    label="Load material",
                    resource_kinds=["material_source"],
                    parameters_schema=_object(
                        {"tool_id": {"enum": [f"tool:{index}" for index in range(count)]}}, ["tool_id"]
                    ),
                    result_schema={"type": "boolean"},
                ),
                OperationDescriptor(
                    id="material.unload",
                    label="Unload material",
                    resource_kinds=["material_source"],
                    result_schema={"type": "boolean"},
                ),
            ]
        )
    for operation, label in (
        ("job.pause", "Pause"),
        ("job.resume", "Resume"),
        ("job.stop", "Stop"),
        ("telemetry.refresh", "Refresh status"),
    ):
        operations.append(
            OperationDescriptor(id=operation, label=label, result_schema={"type": "boolean"}, idempotent=True)
        )
    operations.extend(_extended_operations())
    topology = [
        {"id": item.id, "kind": item.kind, "parent": item.parent_id, "relations": item.relations}
        for item in sorted(resources, key=lambda item: item.id)
    ]
    revision = hashlib.sha256(json.dumps(topology, sort_keys=True).encode()).hexdigest()[:16]
    return DeviceDescriptor.model_validate(
        {
            "device_id": config.serial_number or f"printer:{config.printer_id}",
            "model_id": config.model,
            "revision": f"bambu:{version}:{config.model or 'unknown'}:{revision}",
            "resources": resources,
            "operations": operations,
            "configuration_schema": {"type": "object"},
            "capabilities": ["printing", "files", "camera"],
            "extensions": {"connection_fields": connection_fields(), "compatibility_ui": "bambu-v1"},
        }
    )


def connection_fields():
    return {
        "ip_address": {"label": "IP address or hostname", "required": True, "maxLength": 253},
        "serial_number": {"label": "Serial number", "required": True, "maxLength": 50},
        "access_code": {"label": "LAN access code", "required": True, "secret": True, "minLength": 1, "maxLength": 20},
    }


def handlers(connection):
    def heat(request):
        target = request.parameters["target"]
        if request.resource_id == "heater:bed":
            return connection.set_bed_temperature(target)
        if request.resource_id == "heater:chamber":
            return connection.set_chamber_temperature(target)
        return connection.set_nozzle_temperature(target, int(request.resource_id.split(":")[-1]))

    def fan(request):
        wire_id = {"part": 1, "aux": 2, "chamber": 3, "aux2": 10}[request.resource_id.split(":")[-1]]
        return connection.set_fan_speed(wire_id, round(request.parameters["speed"] * 255 / 100))

    def jog(request):
        axis = request.resource_id.split(":")[-1].upper()
        return connection.send_gcode(
            f"G91\nG0 {axis}{request.parameters['distance']} F{request.parameters.get('speed', 600)}\nG90"
        )

    def material_wire(resource_id):
        _, _, unit, slot = resource_id.split(":")
        unit, slot = int(unit), int(slot)
        return unit if unit >= 128 else unit * 4 + slot

    def start(request):
        from backend.app.printers.sdk import JobRequest

        job = JobRequest.model_validate(request.parameters)
        options = dict(job.options)
        wire_mapping = []
        if job.bindings:
            maximum = max(int(binding.input_id) for binding in job.bindings)
            wire_mapping = [-1] * maximum
            for binding in job.bindings:
                if binding.source_id is not None:
                    wire_mapping[int(binding.input_id) - 1] = material_wire(binding.source_id)
        return connection.start_print(
            job.artifact.uri, plate_id=int(job.plate_id or 1), ams_mapping=wire_mapping or None, **options
        )

    result = {
        "heater.set_target": heat,
        "tool.select": lambda req: connection.select_extruder(int(req.resource_id.split(":")[-1])),
        "fan.set_speed": fan,
        "light.set": lambda req: connection.set_chamber_light(req.parameters["on"]),
        "motion.jog": jog,
        "motion.home": lambda req: connection.send_gcode("G28 " + " ".join(req.parameters.get("axes", []))),
        "job.speed": lambda req: connection.set_print_speed(req.parameters["mode"]),
        "job.pause": lambda _req: connection.pause_print(),
        "job.resume": lambda _req: connection.resume_print(),
        "job.stop": lambda _req: connection.stop_print(),
        "telemetry.refresh": lambda _req: connection.request_status_update(),
        "material.load": lambda req: connection.ams_load_filament(
            material_wire(req.resource_id), int(req.parameters["tool_id"].split(":")[-1])
        ),
        "material.unload": lambda req: connection.ams_unload_filament(material_wire(req.resource_id)),
        "job.start": start,
    }
    for name in _EXTENDED:

        def execute(request, name=name):
            from fastapi.encoders import jsonable_encoder
            from pydantic import TypeAdapter

            method = getattr(connection, name)
            annotations = get_type_hints(method)
            parameters = {
                key: TypeAdapter(annotations[key]).validate_python(value) if key in annotations else value
                for key, value in request.parameters.items()
            }
            value = method(**parameters)
            import inspect

            if inspect.isawaitable(value):

                async def encoded():
                    return jsonable_encoder(await value)

                return encoded()
            return jsonable_encoder(value)

        result[f"bambu.{name}"] = execute
    return result


_EXTENDED = (
    "send_gcode",
    "set_airduct_mode",
    "set_xcam_option",
    "set_ams_filament_backup",
    "start_calibration",
    "clear_hms_errors",
    "skip_objects",
    "send_drying_command",
    "get_kprofiles",
    "set_kprofile",
    "set_kprofiles_batch",
    "delete_kprofile",
    "ams_refresh_tray",
    "ams_set_filament_setting",
    "reset_ams_slot",
    "extrusion_cali_sel",
    "extrusion_cali_set",
    "execute_hms_action",
)


def _extended_operations():
    from pydantic import TypeAdapter

    from .mqtt import BambuMQTTClient

    result = []
    for name in _EXTENDED:
        function = getattr(BambuMQTTClient, name)
        annotations = get_type_hints(function)
        properties, required, definitions = {}, [], {}
        for parameter in signature(function).parameters.values():
            if parameter.name == "self":
                continue
            if parameter.kind in {Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD}:
                continue
            annotation = annotations.get(parameter.name, Any)
            schema = TypeAdapter(annotation).json_schema()
            definitions.update(schema.pop("$defs", {}))
            properties[parameter.name] = schema
            if parameter.default is Parameter.empty:
                required.append(parameter.name)
        schema = _object(properties, required)
        if definitions:
            schema["$defs"] = definitions
        result.append(
            OperationDescriptor(
                id=f"bambu.{name}",
                namespace="bambu",
                label=name.replace("_", " ").title(),
                permission="printers:ams_rfid" if name == "ams_refresh_tray" else "printers:control",
                parameters_schema=schema,
                result_schema=TypeAdapter(annotations.get("return", Any)).json_schema(),
            )
        )
    return result


def snapshot(config, connection, descriptor):
    state = connection.state
    values = []
    for resource in descriptor.resources:
        properties = {}
        if resource.kind == "tool":
            properties["selected"] = TelemetryValue(value=state.active_extruder == int(resource.id.split(":")[-1]))
        elif resource.kind == "heater":
            suffix = resource.id.split(":")[-1]
            keys = (
                ("bed", "bed_target")
                if suffix == "bed"
                else ("chamber", "chamber_target")
                if suffix == "chamber"
                else ("nozzle", "nozzle_target")
                if suffix == "0"
                else ("nozzle_2", "nozzle_2_target")
            )
            for prop, key in zip(("current", "target"), keys, strict=True):
                value = state.temperatures.get(key)
                properties[prop] = TelemetryValue(
                    value=value, unit="degC", quality="known" if value is not None else "unknown"
                )
        elif resource.kind == "light":
            properties["on"] = TelemetryValue(
                value=state.chamber_light == "on" if isinstance(state.chamber_light, str) else bool(state.chamber_light)
            )
        elif resource.kind == "fan":
            attr = {
                "part": "cooling_fan_speed",
                "aux": "big_fan1_speed",
                "chamber": "big_fan2_speed",
                "aux2": "left_aux_fan_speed",
            }[resource.id.split(":")[-1]]
            value = getattr(state, attr, None)
            properties["speed"] = TelemetryValue(
                value=value, unit="%", quality="known" if value is not None else "unknown"
            )
        elif resource.kind == "material_source":
            _, _, unit_id, slot_id = resource.id.split(":")
            units = state.raw_data.get("ams", [])
            tray = next(
                (
                    tray
                    for unit in units
                    for tray in unit.get("tray", [])
                    if str(unit.get("id", 0)) == unit_id and str(tray.get("id", 0)) == slot_id
                ),
                {},
            )
            for prop, key in (("material", "tray_type"), ("color", "tray_color"), ("remaining", "remain")):
                value = tray.get(key)
                if prop == "remaining" and isinstance(value, (int, float)) and value < 0:
                    value = None
                properties[prop] = TelemetryValue(
                    value=value,
                    unit="%" if prop == "remaining" else None,
                    quality="known" if value is not None else "unknown",
                )
        values.append(ResourceState(resource_id=resource.id, available=state.connected, properties=properties))
    return DeviceSnapshot(
        device_id=descriptor.device_id,
        descriptor_revision=descriptor.revision,
        connected=state.connected,
        status=state.state,
        job_id=state.subtask_id,
        resources=values,
    )

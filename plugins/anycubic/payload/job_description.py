"""Sliced-file requirements and declarative print options for SDK 2.2."""

from backend.app.printers.contracts import PrinterPluginError
from backend.app.printers.sdk import JobDescription, JobInput

from .printing import inspect_print_file
from .protocol import AnycubicError

_FLAGS = {
    "auto_leveling": ("Bed leveling", "Level the bed before printing.", "defaultBedLevelling"),
    "flow_calibration": ("Flow calibration", "Calibrate flow before printing.", "defaultFlowCali"),
    "vibration_compensation": (
        "Vibration calibration",
        "Calibrate vibration compensation before printing.",
        "defaultVibrationCali",
    ),
    "timelapse": ("Timelapse", "Record a timelapse of the print.", "defaultTimelapse"),
}


def options_schema(native=None, *, require_feeder=False):
    supported = set(native.control_capabilities().get("print_options", [])) if native is not None else set(_FLAGS)
    use_feeder = {"type": "boolean", "title": "Use ACE", "default": True}
    if require_feeder:
        use_feeder["const"] = True
    properties = {"use_ace": use_feeder}
    for key, (label, description, translation) in _FLAGS.items():
        properties[key] = {
            "type": "boolean",
            "title": label,
            "description": description,
            "default": False,
            "x-label-key": "settings." + translation,
            "x-description-key": "settings." + translation + "Desc",
        }
        if key not in supported:
            properties[key].update(const=False, readOnly=True)
    return {"type": "object", "title": "Print options", "properties": properties, "additionalProperties": False}


def describe_job(plugin, config, artifact, path):
    try:
        parsed = inspect_print_file(path, artifact.filename or path.name, config.model)
    except AnycubicError as exc:
        raise PrinterPluginError(str(exc)) from exc
    connection = plugin.connections.get(config.serial_number)
    native = connection.native if connection is not None else None
    device = plugin.describe_device(config)
    sources = [resource.id for resource in device.resources if resource.kind == "material_source"]
    if len(parsed.filaments) > 1:
        sources = [source for source in sources if source != "material:external"]
    usage = {
        item["tool"]: item["grams"]
        for item in parsed.estimates.get("anycubic_tool_usage", [])
        if isinstance(item, dict) and "tool" in item and "grams" in item
    }
    inputs = []
    for filament in parsed.filaments:
        color = "".join(f"{component:02X}" for component in filament.color)
        inputs.append(
            JobInput(
                id=f"T{filament.tool}",
                label=f"Filament {filament.tool + 1}",
                artifact_uri=artifact.uri,
                routing_key=f"{filament.material_type.strip().lower()}:{color}",
                material_type=filament.material_type,
                color="#" + color,
                required_mass_grams=usage.get(filament.tool),
                compatible_tool_ids=["tool:primary"],
                compatible_source_ids=sources,
            )
        )
    schema = options_schema(native, require_feeder=len(inputs) > 1)
    return JobDescription(
        artifact=artifact.model_copy(update={"sha256": parsed.file.sha256}),
        inputs=inputs,
        options_schema=schema,
        default_options={key: value["default"] for key, value in schema["properties"].items()},
        extensions={
            "bambuddy:ui": {
                "material_mode": {
                    "option": "use_ace",
                    "external_source_id": "material:external",
                    "require_reported_material": True,
                    "source_placeholder": "Select ACE slot",
                    "enabled_help": "Map each sliced filament to an available ACE slot.",
                    "disabled_help": "Load the sliced filament on the external spool holder.",
                }
            }
        },
    )

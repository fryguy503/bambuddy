"""Stock LAN print codec and authoritative sliced-file material requirements.

The independent codec follows the previously audited Slicer Next 2.0.0.3
LAN_SendRemotePrint serializer. A serialized request is not print acceptance.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .files import MAX_LINE_BYTES, GcodeArtifact, validate_gcode
from .protocol import AnycubicError

OPTION_FEATURES = {
    "auto_leveling": "auto_leveling_support",
    "vibration_compensation": "vibration_compensation_support",
    "flow_calibration": "flow_calibration_support",
    "timelapse": "camera_timelapse_support",
}
OPTION_KEYS = {*OPTION_FEATURES, "use_ace", "filament_mapping"}
_FIELD = re.compile(r";\s*(filament_type|filament_colou?r|filament used \[mm\])\s*=\s*(.*)", re.I)
_TOOL = re.compile(rb"(?:N\d+\s+)?T(\d+)(?=\s|;|\*|$)", re.I)


def rgba(value):
    if not isinstance(value, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
        raise AnycubicError("Sliced filament colors must be RGB or RGBA")
    value = value.removeprefix("#")
    result = tuple(int(value[i : i + 2], 16) for i in range(0, len(value), 2))
    return result if len(result) == 4 else (*result, 255)


@dataclass(frozen=True)
class PrintFilament:
    tool: int
    material_type: str
    color: tuple[int, ...]


@dataclass(frozen=True)
class PrintArtifact:
    file: GcodeArtifact
    md5: str
    filaments: tuple[PrintFilament, ...]

    def public(self):
        return {
            "model": self.file.model,
            "tools": [
                {"tool": item.tool, "type": item.material_type, "color": "".join(f"{c:02X}" for c in item.color)}
                for item in self.filaments
            ],
        }


def inspect_print_file(path: Path, filename: str, model: str | None = None) -> PrintArtifact:
    """Read the complete file, retaining original zero-based slicer tool IDs."""
    artifact = validate_gcode(path, filename, model)
    fields, tools = {}, set()
    md5, sha256, size = hashlib.md5(usedforsecurity=False), hashlib.sha256(), 0
    with path.open("rb") as stream:
        while line := stream.readline(MAX_LINE_BYTES + 1):
            size += len(line)
            if len(line) > MAX_LINE_BYTES or size > artifact.size:
                raise AnycubicError("The G-code changed while inspecting its filament metadata")
            md5.update(line)
            sha256.update(line)
            stripped = line.strip()
            if match := _TOOL.match(stripped):
                if len(match[1]) > 3 or int(match[1]) > 255:
                    raise AnycubicError("The G-code contains an unsupported tool selection")
                tools.add(int(match[1]))
            if not stripped.startswith(b";"):
                continue
            match = _FIELD.fullmatch(stripped.decode("utf-8-sig"))
            if not match:
                continue
            key, value = match[1].lower(), match[2].strip()
            key = "filament_colour" if key in {"filament_color", "filament_colour"} else key
            if len(value) > 16384 or key in fields and fields[key] != value:
                raise AnycubicError("The G-code contains conflicting or oversized filament metadata")
            fields[key] = value
    if (size, sha256.hexdigest()) != (artifact.size, artifact.sha256):
        raise AnycubicError("The G-code changed while inspecting its filament metadata")
    materials = [part.strip().strip('"') for part in fields.get("filament_type", "").split(";")]
    colors = [part.strip().strip('"') for part in fields.get("filament_colour", "").split(";")]
    if not 1 <= len(materials) <= 256 or not 1 <= len(colors) <= 256:
        raise AnycubicError("Export G-code with filament_type and filament_colour metadata")
    used = set(range(max(len(materials), len(colors))))
    if "filament used [mm]" in fields:
        try:
            amounts = [float(part.strip()) for part in fields["filament used [mm]"].split(",")]
            if not 1 <= len(amounts) <= 256 or any(not math.isfinite(value) or value < 0 for value in amounts):
                raise ValueError
            used = {index for index, amount in enumerate(amounts) if amount > 0}
        except ValueError:
            raise AnycubicError("The sliced filament usage vector is invalid") from None
    required = sorted(tools | used)
    if not required:
        raise AnycubicError("The sliced file does not identify a used filament")
    result = []
    for tool in required:
        if tool >= len(materials) or tool >= len(colors):
            raise AnycubicError("A G-code tool has no matching sliced filament metadata")
        material = materials[tool]
        if (
            not material
            or len(material) > 64
            or material.upper() in {"?", "UNKNOWN", "UNKONWN", "DEFAULT"}
            or any(ord(char) < 32 or ord(char) == 127 for char in material)
        ):
            raise AnycubicError("A used filament has no valid sliced material type")
        result.append(PrintFilament(tool, material, rgba(colors[tool])))
    return PrintArtifact(artifact, md5.hexdigest(), tuple(result))


def validate_options(options):
    if not isinstance(options, dict) or set(options) - OPTION_KEYS:
        raise AnycubicError("Unsupported Anycubic print settings")
    value: dict = dict.fromkeys(OPTION_FEATURES, False)
    value.update(use_ace=False, filament_mapping=[])
    value.update(options)
    if any(type(value[key]) is not bool for key in (*OPTION_FEATURES, "use_ace")):
        raise AnycubicError("Anycubic print settings must use explicit boolean choices")
    rows = value["filament_mapping"]
    if not isinstance(rows, list) or len(rows) > 256:
        raise AnycubicError("Invalid ACE filament mapping")
    tools = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"tool", "box_id", "slot_id"}
            or any(type(row[key]) is not int for key in row)
            or not 0 <= row["tool"] <= 255
            or row["tool"] in tools
            or not -1 <= row["box_id"] <= 3
            or row["slot_id"] not in range(4)
        ):
            raise AnycubicError("Invalid or duplicate physical ACE mapping")
        tools.add(row["tool"])
    if value["use_ace"] != bool(rows):
        raise AnycubicError("ACE selection requires a mapping for every sliced filament")
    return value


def print_data(artifact: PrintArtifact, options: dict, slots: list[dict], supported_options: list[str]):
    """Resolve selected physical slots from current inventory, never UI ordinals."""
    value = validate_options(options)
    if any(value[key] and key not in supported_options for key in OPTION_FEATURES):
        raise AnycubicError("A selected print option is not supported by the current printer")
    selected = {item["tool"]: item for item in value["filament_mapping"]}
    required = {item.tool for item in artifact.filaments}
    mappings = []
    if value["use_ace"]:
        if set(selected) != required:
            raise AnycubicError("Map every original sliced tool to an available ACE slot")
        for filament in artifact.filaments:
            choice = selected[filament.tool]
            matches = [s for s in slots if (s["box_id"], s["slot_id"]) == (choice["box_id"], choice["slot_id"])]
            if len(matches) != 1 or not matches[0]["available"]:
                raise AnycubicError("A selected ACE slot is unavailable or its material status is stale")
            slot = matches[0]
            if slot["material_type"].casefold() != filament.material_type.casefold():
                raise AnycubicError("The selected ACE material does not match the sliced filament type")
            mappings.append(
                {
                    "paint_index": filament.tool,
                    "ams_index": slot["ams_index"],
                    "paint_color": list(filament.color),
                    "ams_color": slot["color"],
                    "material_type": filament.material_type,
                }
            )
    elif len(required) != 1:
        raise AnycubicError("A multi-filament print requires an ACE mapping for every used tool")
    empty_capture = {"status": 0, "count": 0, "type": 0, "mode": 0}
    return {
        "taskid": "-1",
        "url": None,
        "filename": artifact.file.filename,
        "md5": artifact.md5,
        "filepath": None,
        "filetype": 1,
        "project_type": 1,
        "filesize": artifact.file.size,
        "ams_settings": {
            "use_ams": value["use_ace"],
            "padding1": 0,
            "padding2": 0,
            "padding3": 0,
            "ams_box_mapping": mappings,
        },
        "task_settings": {
            "auto_leveling": int(value["auto_leveling"]),
            "vibration_compensation": int(value["vibration_compensation"]),
            "flow_calibration": int(value["flow_calibration"]),
            "dry_mode": 0,
            "ai_settings": dict(empty_capture),
            "timelapse": {**empty_capture, "status": int(value["timelapse"])},
            "drying_settings": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
            "model_objects_skip_parts": [],
        },
    }

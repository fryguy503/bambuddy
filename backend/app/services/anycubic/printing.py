"""Stock LAN print codec and authoritative sliced-file material requirements.

The independent codec follows the previously audited Slicer Next 2.0.0.3
LAN_SendRemotePrint serializer. A serialized request is not print acceptance.
"""

import hashlib
import math
import re
from dataclasses import dataclass, field
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
_ESTIMATE = re.compile(
    r";\s*(filament used \[g\]|total filament used \[g\]|estimated printing time \(normal mode\))\s*=\s*(.*)",
    re.I,
)
_DURATION = re.compile(r"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s\s*)?", re.I)
ESTIMATES_VERSION = 1
ESTIMATE_KEYS = frozenset(
    {
        "print_time_seconds",
        "filament_used_grams",
        "filament_used_mm",
        "filament_slots",
        "anycubic_tool_usage",
        "anycubic_estimates_version",
        "anycubic_estimates_sha256",
        "anycubic_estimates_error",
    }
)


def merge_estimates(existing: dict | None, estimates: dict) -> dict:
    """Replace derived values without retaining stale amounts or unrelated edits."""
    return {**{key: value for key, value in (existing or {}).items() if key not in ESTIMATE_KEYS}, **estimates}


def _amounts(value: str | None) -> list[float] | None:
    if value is None:
        return None
    try:
        values = [float(part.strip()) for part in value.split(",")]
    except ValueError:
        return None
    if not 1 <= len(values) <= 256 or any(not math.isfinite(amount) or amount < 0 for amount in values):
        return None
    return values if math.isfinite(sum(values)) else None


def _estimates(artifact: GcodeArtifact, optional: dict, lengths: list[float] | None, filaments) -> dict:
    result: dict = {"anycubic_estimates_version": ESTIMATES_VERSION, "anycubic_estimates_sha256": artifact.sha256}
    duration = optional.get("estimated printing time (normal mode)")
    if duration and (match := _DURATION.fullmatch(duration)) and any(match.groups()):
        try:
            seconds = sum(
                int(value or 0) * factor for value, factor in zip(match.groups(), (86400, 3600, 60, 1), strict=True)
            )
            if 0 <= seconds <= (1 << 31) - 1:
                result["print_time_seconds"] = seconds
        except ValueError:
            pass  # Optional malformed/oversized estimates remain unknown.
    if lengths is not None and math.isfinite(sum(lengths)):
        result["filament_used_mm"] = sum(lengths)
    grams = _amounts(optional.get("filament used [g]"))
    total_values = _amounts(optional.get("total filament used [g]"))
    if "filament used [g]" in optional and grams is None:
        return result
    if "total filament used [g]" in optional and (total_values is None or len(total_values) != 1):
        return result
    total = total_values[0] if total_values else None
    if grams is not None:
        if total is not None and not math.isclose(
            sum(grams), total, rel_tol=0, abs_tol=0.005 * (len(grams) + 1) + 1e-9
        ):
            return result
        result["filament_used_grams"] = total if total is not None else sum(grams)
        tools = {entry.tool for entry in filaments}
        if any(tool >= len(grams) for tool in tools) or any(
            value > 0 and tool not in tools for tool, value in enumerate(grams)
        ):
            return result  # An aggregate can be displayed without inventing a physical-tool allocation.
        result["anycubic_tool_usage"] = [{"tool": entry.tool, "grams": grams[entry.tool]} for entry in filaments]
        result["filament_slots"] = [
            {
                "slot_id": entry.tool + 1,
                "used_g": grams[entry.tool],
                "type": entry.material_type,
                "color": "#" + "".join(f"{component:02X}" for component in entry.color),
            }
            for entry in filaments
        ]
    elif total is not None:
        result["filament_used_grams"] = total
    return result


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
    estimates: dict = field(default_factory=dict)

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
    fields, tools, optional = {}, set(), {}
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
            decoded = stripped.decode("utf-8-sig")
            if estimate := _ESTIMATE.fullmatch(decoded):
                key, value = estimate[1].lower(), estimate[2].strip()
                optional[key] = None if len(value) > 16384 or key in optional and optional[key] != value else value
            match = _FIELD.fullmatch(decoded)
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
    amounts = None
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
    return PrintArtifact(artifact, md5.hexdigest(), tuple(result), _estimates(artifact, optional, amounts, result))


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

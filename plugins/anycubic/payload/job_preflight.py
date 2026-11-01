"""Read-only queue material checks; only dispatch validates permission to publish."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

from backend.app.printers.sdk import JobRequest, validate_job_request

from .device import native_slot
from .printing import MaterialMappingError, inspect_print_file, print_data, validate_options
from .protocol import AnycubicError

_requirements = OrderedDict()
_requirements_lock = threading.Lock()
_MAX_REQUIREMENTS = 32


class MaterialSelectionRequired(AnycubicError):
    """Created only before reserving a dispatch; safe for the host to keep pending."""

    def __init__(self, issues):
        super().__init__(issues[0]["message"])
        self.issues = issues


def inspect_requirements(path, filename, model, *, expected_sha256=None):
    """Cache advisory requirements by source identity; never cache start permission."""
    path = Path(path).resolve(strict=True)
    before = path.stat()
    signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    key = (str(path), filename, model, expected_sha256)
    with _requirements_lock:
        cached = _requirements.get(key)
        if cached is not None and cached[0] == signature:
            _requirements.move_to_end(key)
            return cached[1]
    parsed = inspect_print_file(path, filename, model)
    if expected_sha256 is not None and parsed.file.sha256 != expected_sha256:
        raise AnycubicError("The queued artifact changed; inspect its print requirements again")
    after = path.stat()
    if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise AnycubicError("The queued artifact changed while reading its print requirements")
    with _requirements_lock:
        _requirements[key] = (signature, parsed)
        _requirements.move_to_end(key)
        while len(_requirements) > _MAX_REQUIREMENTS:
            _requirements.popitem(last=False)
    return parsed


def prepare_job(job, artifact):
    """Use the same original-tool and physical-slot plan for preflight and start."""
    mapping, inputs = [], {}
    required = {filament.tool for filament in artifact.filaments}
    numeric = all(binding.input_id.isdecimal() for binding in job.bindings)
    numeric_ids = {int(binding.input_id) for binding in job.bindings} if numeric else set()
    offset = 1 if numeric_ids != required and numeric_ids == {tool + 1 for tool in required} else 0
    for binding in job.bindings:
        match = re.fullmatch(r"(?:T|input:)?(\d+)", binding.input_id)
        if not match:
            raise MaterialMappingError("Material input IDs must identify original sliced T inputs")
        tool = int(match[1]) - offset
        if tool in inputs.values():
            raise MaterialMappingError("Each sliced input requires exactly one material binding", tools=[tool])
        inputs[binding.input_id] = tool
        if binding.source_id == "material:external":
            continue
        if not (binding.source_id or binding.slot_id):
            raise MaterialMappingError("Select an ACE slot for every sliced material input", tools=[tool])
        box, slot = native_slot(binding.source_id or binding.slot_id or "")
        mapping.append({"tool": tool, "box_id": box, "slot_id": slot})
    if job.bindings and set(inputs.values()) != required:
        raise MaterialMappingError("Bind every original sliced material input exactly once", tools=required)
    options = validate_options(
        {
            "use_ace": bool(mapping),
            "filament_mapping": mapping,
            **{
                name: value
                for name, value in job.options.items()
                if name
                in {
                    "auto_leveling",
                    "vibration_compensation",
                    "flow_calibration",
                    "timelapse",
                    "allow_material_type_mismatch",
                }
            },
        }
    )
    if set(job.options) - {
        "auto_leveling",
        "vibration_compensation",
        "flow_calibration",
        "timelapse",
        "use_ace",
        "allow_material_type_mismatch",
    }:
        raise AnycubicError("Unsupported native print option")
    if "use_ace" in job.options and job.options["use_ace"] != bool(mapping):
        raise MaterialMappingError("ACE selection must agree with the explicit material bindings", tools=required)
    return options, inputs


def material_issue(error, job, inputs=None):
    affected = set(error.tools)
    input_ids = [
        binding.input_id for binding in job.bindings if not affected or (inputs or {}).get(binding.input_id) in affected
    ]
    return {
        "code": error.code,
        "message": str(error),
        "recovery": "material_selection",
        "input_ids": input_ids,
        "resource_ids": list(error.resource_ids),
    }


def _issue(code, message, recovery, bindings):
    return {
        "code": code,
        "message": message,
        "recovery": recovery,
        "input_ids": [binding.input_id for binding in bindings],
        "resource_ids": sorted(
            {binding.source_id or binding.slot_id for binding in bindings if binding.source_id or binding.slot_id}
        ),
    }


def material_readiness(native, job):
    """Distinguish a selected empty slot from uncertain current capabilities."""
    ace_bindings = [binding for binding in job.bindings if binding.source_id != "material:external"]
    # A missing or stale selected material can be reported without reopening a
    # large source file on every queue poll. Remaining percentages are estimates,
    # so 0% alone never means an otherwise loaded slot has run out.
    with native._lock:
        fresh = native._fresh_connection()
        capabilities = deepcopy(native.print_capabilities())
        boxes = deepcopy(native._telemetry.boxes)
        seen, material_seen = dict(native._ace_seen), dict(native._ace_material_seen)
    if not fresh:
        return capabilities, [
            _issue(
                "anycubic.material_telemetry_stale",
                "Waiting for fresh printer and ACE material information",
                "refresh",
                ace_bindings,
            )
        ]
    for binding in ace_bindings:
        if not (binding.source_id or binding.slot_id):
            return capabilities, [
                _issue(
                    "anycubic.material_mapping_required",
                    "Select an ACE slot for every sliced material input",
                    "material_selection",
                    [binding],
                )
            ]
        box_id, slot_id = native_slot(binding.source_id or binding.slot_id or "")
        now = time.monotonic()
        box = boxes.get(box_id)
        box_fresh = box is not None and all(
            observed is not None and 0 <= now - observed <= native.STALE_TIMEOUT
            for observed in (seen.get(box_id), material_seen.get(box_id))
        )
        if (
            box is not None
            and not box_fresh
            or box is None
            and not any(0 <= now - observed <= native.STALE_TIMEOUT for observed in seen.values())
        ):
            return capabilities, [
                _issue(
                    "anycubic.material_telemetry_stale",
                    "Waiting for fresh selected ACE material information",
                    "refresh",
                    [binding],
                )
            ]
        matches = [slot for slot in capabilities["ace_slots"] if (slot["box_id"], slot["slot_id"]) == (box_id, slot_id)]
        trays = [tray for tray in box.get("tray", []) if tray.get("id") == slot_id] if box is not None else []
        available = bool(
            box_fresh
            and len(trays) == 1
            and trays[0].get("state") == 3
            and len(matches) == 1
            and matches[0]["material_type"]
            and matches[0]["color"]
        )
        if not available:
            return capabilities, [
                _issue(
                    "anycubic.material_unavailable",
                    "A selected ACE slot is empty or unavailable; review the filament mapping",
                    "material_selection",
                    [binding],
                )
            ]
        if matches[0]["available"] is not True:
            # Native topology freshness can depend on a sibling unit. A known
            # loaded selected slot is not a wrong user selection in that case.
            return capabilities, [
                _issue(
                    "anycubic.material_capabilities_stale",
                    "Waiting for fresh ACE capability information",
                    "refresh",
                    [binding],
                )
            ]
    return capabilities, []


async def preflight_job(plugin, config, connection, job, path):
    """No pairing, upload, journal reservation, inventory change or printer command."""
    job = validate_job_request(plugin.describe_device(config), JobRequest.model_validate(job))
    if set(job.selected_tool_ids) != {"tool:primary"}:
        raise AnycubicError("Select this printer's primary physical tool")
    capabilities, issues = material_readiness(connection.native, job)
    if issues:
        return {"issues": issues}
    artifact = await asyncio.to_thread(
        inspect_requirements,
        path,
        job.artifact.filename or Path(path).name,
        config.model,
        expected_sha256=job.artifact.sha256,
    )
    # A large first inspection may outlive the material snapshot above. Read
    # current telemetry again before declaring this pending job ready.
    capabilities, issues = material_readiness(connection.native, job)
    if issues:
        return {"issues": issues}
    inputs = {}
    try:
        options, inputs = prepare_job(job, artifact)
        print_data(artifact, options, capabilities["ace_slots"], capabilities["options"])
    except MaterialMappingError as error:
        return {"issues": [material_issue(error, job, inputs)]}
    return {"issues": []}

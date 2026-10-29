"""Validate vendor/file/settings compatibility before a queue row is written."""

import asyncio
import json
from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.core.config import settings
from backend.app.schemas.anycubic_print import AnycubicPrintOptions
from backend.app.utils.anycubic_models import get_model

from .protocol import AnycubicError


def options_dict(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid stored Anycubic print settings") from None
    try:
        return AnycubicPrintOptions.model_validate(value).model_dump()
    except ValidationError:
        raise HTTPException(400, "Choose valid Anycubic print settings and physical ACE slots") from None


async def validate_source(printer, library_file, archive, options, *, target_model=None, values=None):
    """Return normalized native options, or None for an ordinary Bambu source.

    File bytes, rather than editable library metadata, identify a native slice.
    This has no network activity and does not require an idle printer to queue.
    """
    native = printer is not None and printer.printer_type == "anycubic"
    raw = any(source is not None and source.filename.lower().endswith(".gcode") for source in (library_file, archive))
    if target_model and get_model(target_model):
        raise HTTPException(400, "Choose a specific Anycubic printer and its physical ACE slots")
    if not native:
        if options is not None or raw:
            raise HTTPException(400, "Raw Anycubic G-code requires a specific matching Anycubic printer")
        return None
    assert printer is not None
    if archive is not None or library_file is None or not raw or target_model:
        raise HTTPException(400, "Anycubic queue printing requires a sliced .gcode library file and a specific printer")
    if get_model(printer.model) is None or not printer.is_active:
        raise HTTPException(400, "Choose an active, supported Anycubic printer")
    if options is None:
        raise HTTPException(400, "Choose Anycubic print settings before adding this file to the queue")
    data = values or {}
    if any(data.get(field) for field in ("ams_mapping", "nozzle_mapping", "nozzle_rack_choice", "filament_overrides")):
        raise HTTPException(400, "Bambu filament and nozzle mappings do not apply to an Anycubic print")
    if (
        data.get("gcode_injection")
        or data.get("preheat_override") not in (None, "off", "inherit")
        or data.get("preheat_chamber_target_override") is not None
        or data.get("cleanup_library_after_dispatch")
        or data.get("confirm_outcome")
        or data.get("cost_center_id") is not None
        or data.get("auto_off_after")
        or data.get("plate_id") not in (None, 1)
    ):
        raise HTTPException(
            400, "This Anycubic print does not support Bambu preheat, injection, plates or archive options"
        )
    normalized = options_dict(options)
    from .printing import inspect_print_file

    stored_path = Path(library_file.file_path)
    path = stored_path if stored_path.is_absolute() else settings.base_dir / stored_path
    try:
        artifact = await asyncio.to_thread(inspect_print_file, path, library_file.filename, printer.model)
    except (AnycubicError, OSError) as exc:
        detail = str(exc) if isinstance(exc, AnycubicError) else "The queued G-code file is unavailable"
        raise HTTPException(400, detail) from None
    tools = {entry.tool for entry in artifact.filaments}
    mappings = normalized["filament_mapping"]
    if normalized["use_ace"]:
        if {entry["tool"] for entry in mappings} != tools:
            raise HTTPException(400, "Map every used G-code tool to a physical ACE slot")
        model = get_model(printer.model)
        assert model is not None
        if any(entry["box_id"] not in model.ace_ids for entry in mappings):
            raise HTTPException(400, "The selected ACE box does not belong to this printer model")
    elif len(tools) != 1:
        raise HTTPException(400, "A multi-filament G-code file requires ACE mapping")
    return normalized

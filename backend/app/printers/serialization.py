"""Common printer snapshots and per-plugin serialization dispatch."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING

from backend.app.printers.api import config_for_id
from backend.app.printers.contracts import PrinterPlugin
from backend.app.printers.models import _provider
from backend.app.printers.registry import get_plugin, plugin_for
from backend.app.services.pause_eta import next_pause

if TYPE_CHECKING:
    from backend.app.printers.state import PrinterState


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def common_state_to_dict(
    state: PrinterState,
    plugin: PrinterPlugin,
    *,
    printer_id: int | None = None,
    model: str | None = None,
) -> dict:
    """Serialize normalized shared fields; vendor raw_data is never read."""
    capabilities = plugin.model_capabilities(model)
    temperatures = dict(state.temperatures or {})
    if "chamber_temperature" not in capabilities:
        temperatures = {key: value for key, value in temperatures.items() if not key.startswith("chamber")}
    snapshot = {
        "connected": state.connected,
        "state": state.state,
        "current_print": state.current_print,
        "subtask_name": state.subtask_name,
        "gcode_file": state.gcode_file,
        "progress": state.progress,
        "remaining_time": state.remaining_time,
        "layer_num": state.layer_num,
        "total_layers": state.total_layers,
        "temperatures": temperatures,
        "hms_errors": [_plain(error) for error in state.hms_errors],
        "ams": list(state.filament_units),
        "ams_exists": bool(state.filament_units),
        "vt_tray": list(state.external_spools),
        "nozzles": [_plain(nozzle) for nozzle in state.nozzles],
        "print_options": _plain(state.print_options),
        "firmware_version": state.firmware_version,
        "wifi_signal": state.wifi_signal,
        "wired_network": state.wired_network,
        "door_open": state.door_open,
        "stg_cur": state.stg_cur,
        "stg_cur_name": state.status_name,
        "stg": list(state.stg),
        "current_plate_id": state.active_plate_id,
        "ams_mapping": list(state.print_ams_mapping or []),
        "ams_extruder_map": dict(state.ams_extruder_map),
        "tray_now": state.tray_now,
        "tray_change_log": [list(entry) for entry in state.tray_change_log],
        "next_pause": next_pause(state),
        "printable_objects_count": len(state.printable_objects),
        "supports_drying": "drying" in capabilities,
        "supports_drying_while_printing": "drying_while_printing" in capabilities,
        "drying_screen_only": "drying_screen_only" in capabilities,
        "supports_chamber_heater": "chamber_heater" in capabilities,
        "sdcard": state.sdcard,
        "store_to_sdcard": state.store_to_sdcard,
        "timelapse": state.timelapse,
        "ipcam": state.ipcam,
        "chamber_light": state.chamber_light,
        "active_extruder": state.active_extruder,
        "speed_level": state.speed_level,
        "airduct_mode": state.airduct_mode,
    }
    for field in (
        "cooling_fan_speed",
        "big_fan1_speed",
        "big_fan2_speed",
        "heatbreak_fan_speed",
        "left_aux_fan_speed",
        "exhaust_fan_present",
    ):
        snapshot[field] = getattr(state, field)
    # A dispatched plate is application metadata and is safe to reuse only for
    # the matching running job. Vendor-specific filename conventions stay out.
    if (
        snapshot["current_plate_id"] is None
        and state.dispatched_subtask
        and state.dispatched_subtask == state.subtask_name
    ):
        snapshot["current_plate_id"] = state.dispatched_plate_id
    snapshot["cover_url"] = (
        f"/api/v1/printers/{printer_id}/cover"
        if printer_id and state.state in {"RUNNING", "PAUSE"} and state.gcode_file
        else None
    )
    return snapshot


def _state_plugin(printer_id: int | None, model: str | None, plugin_id: str | None):
    config = config_for_id(printer_id)
    if config is not None:
        return plugin_for(config)
    if plugin_id is not None:
        return get_plugin(plugin_id)
    return _provider(model)


def printer_state_to_dict(
    state: PrinterState,
    printer_id: int | None = None,
    model: str | None = None,
    drying_targets: dict[int, dict] | None = None,
    *,
    plugin_id: str | None = None,
) -> dict:
    plugin = _state_plugin(printer_id, model, plugin_id)
    snapshot = plugin.serialize_state(state, printer_id, model, drying_targets)
    # Plate clearance, naming and model identity belong to core regardless of
    # which plugin implements the hardware protocol.
    from backend.app.services.printer_manager import printer_manager

    snapshot["awaiting_plate_clear"] = printer_manager.is_awaiting_plate_clear(printer_id) if printer_id else False
    snapshot["plate_clear_required"] = (
        printer_manager.provider_requires_plate_clear(printer_id)
        if printer_id
        else getattr(state, "plate_clear_required", False) is True
    )
    if printer_id:
        info = printer_manager.get_printer(printer_id)
        if info is not None:
            snapshot["name"] = info.name
    if model:
        snapshot["model"] = model
    return snapshot


def get_derived_status_name(
    state: PrinterState,
    model: str | None = None,
    *,
    plugin_id: str | None = None,
) -> str | None:
    return _state_plugin(None, model, plugin_id).derive_status(state, model)

"""Bambu protocol state, accessory and stage serialization."""

import math
import re
from collections.abc import Callable
from dataclasses import asdict, is_dataclass

from backend.app.services.pause_eta import next_pause

from .ams_humidity import ams_humidity_percent
from .capabilities import (
    drying_screen_only,
    has_stg_cur_idle_bug,
    supports_chamber_heater,
    supports_chamber_temp,
    supports_drying,
    supports_drying_while_printing,
    uniform_tray_filament_hint,
)
from .kprofile_lookup import build_slot_k_resolver
from .legacy_state import PrinterState
from .mqtt import STAGE_NAMES, get_stage_name


def get_derived_status_name(state: PrinterState, model: str | None = None) -> str | None:
    """
    Compute a human-readable status name based on printer state.

    Uses stg_cur when available, otherwise derives status from temperature data
    when the printer is heating before a print starts.

    Args:
        state: The printer state to analyze
        model: Optional printer model for model-specific workarounds
    """
    # Firmware bug: some models (A1, P1P, P1S) report stg_cur=0 when not printing.
    # stg_cur=0 maps to "Printing" in STAGE_NAMES, which incorrectly overrides the
    # real state (IDLE, FINISH, FAILED, etc.). Only trust stg_cur when the printer
    # is actually in an active print state (RUNNING or PAUSE).
    if state.state not in ("RUNNING", "PAUSE") and state.stg_cur == 0 and has_stg_cur_idle_bug(model):
        return None

    # If we have a valid calibration stage, use it
    # X1 models use -1 for idle, A1/P1 models use 255 for idle
    # Valid stage numbers are 0-254
    if 0 <= state.stg_cur < 255:
        # A stage number the table does not cover is named "Preparing" rather
        # than "Unknown stage (72)". New models report stages before Bambuddy
        # learns their names -- the H2C still has several -- and the card is
        # the wrong place to say so: the number means nothing to the person
        # reading it, and every stage that has ever turned out to be unnamed
        # was part of the run-up to printing, so "Preparing" is both the more
        # useful answer and the more likely one.
        #
        # This is display only, and deliberately not pushed down into
        # `get_stage_name`. That function also feeds the stage-transition log
        # line and the once-per-session warning that exists precisely to
        # capture unnamed stages so they can be named later (bambu_mqtt.py
        # ~4100) -- there the number is the entire diagnostic value, and
        # replacing it with "Preparing" would hide the very thing that
        # reports these.
        if state.stg_cur not in STAGE_NAMES:
            return "Preparing"
        return get_stage_name(state.stg_cur)

    # If not in RUNNING state, no derived status needed
    if state.state != "RUNNING":
        return None

    # Check if we're in an early phase where temperatures are heating
    temps = state.temperatures or {}
    progress = state.progress or 0

    # Only derive heating status when progress is very low (< 2%)
    # This indicates we're in the preparation phase, not actually printing
    if progress >= 2:
        return None

    # Check bed temperature - if target is set and current is significantly below
    bed_temp = temps.get("bed", 0)
    bed_target = temps.get("bed_target", 0)

    # Check nozzle temperature
    nozzle_temp = temps.get("nozzle", 0)
    nozzle_target = temps.get("nozzle_target", 0)

    # Temperature thresholds: consider "heating" if more than 10°C below target
    TEMP_THRESHOLD = 10

    # Determine what's heating (prioritize bed since it takes longer)
    if bed_target > 30 and (bed_target - bed_temp) > TEMP_THRESHOLD:
        return "Heating heatbed"
    elif nozzle_target > 30 and (nozzle_target - nozzle_temp) > TEMP_THRESHOLD:
        return "Heating nozzle"

    # If targets are set but we're close to them, we might be in final prep
    if bed_target > 30 or nozzle_target > 30:
        if progress == 0 and state.layer_num == 0:
            return "Preparing"

    return None


_PLATE_ID_RE = re.compile(r"plate_(\d+)\.gcode")


def parse_plate_id(gcode_file: str | None) -> int | None:
    """Extract the 1-indexed plate number from a Bambu gcode_file path.

    Returns None when the path is missing or has no `plate_N.gcode` segment.
    Shared by the REST status route and the WebSocket push path so both agree
    on the value sent to the frontend (#881 follow-up).
    """
    if not gcode_file:
        return None
    match = _PLATE_ID_RE.search(gcode_file)
    return int(match.group(1)) if match else None


def resolve_plate_id(state) -> int | None:
    """Resolve the active plate number from a PrinterState.

    Some firmware versions (e.g. P1S 01.10.00.00, #1166) put only the .3mf
    filename in print.gcode_file, so parse_plate_id() returns None and the
    printer card falls back to plate 1 — wrong thumbnail. When Bambuddy
    dispatched the print itself we already know the right plate, so we prefer
    that over the gcode_file echo. The subtask check prevents stale values
    from a previous Bambuddy-dispatched print bleeding into a Studio-direct
    print on the same printer.
    """
    dispatched_plate = getattr(state, "dispatched_plate_id", None)
    dispatched_subtask = getattr(state, "dispatched_subtask", None)
    if (
        dispatched_plate is not None
        and dispatched_subtask is not None
        and state.subtask_name
        and dispatched_subtask == state.subtask_name
    ):
        return dispatched_plate
    return parse_plate_id(state.gcode_file)


def print_ams_mapping(state: PrinterState) -> list:
    """The running print's AMS mapping: the one captured from its print command,
    else whatever the printer's push carried (it rarely does)."""
    if isinstance(state.print_ams_mapping, list):
        return list(state.print_ams_mapping)
    raw = (state.raw_data or {}).get("ams_mapping")
    return list(raw) if isinstance(raw, list) else []


def nozzle_wiring(state: PrinterState) -> dict | None:
    """Which nozzle each AMS is wired to, for the runout hint; None with a
    Filament Track Switch fitted, where an AMS can feed either nozzle and the
    map can still hold an AMS's wiring from before it moved onto the switch."""
    if state.fila_switch and state.fila_switch.installed:
        return None
    return state.ams_extruder_map


def _trays_on_extruder(
    raw_slot: int,
    ams_layout: list[tuple[int, bool]],
    active_extruder: int | None,
    ams_extruder_map: dict | None,
) -> set[int] | None:
    """The global trays a local ``raw_slot`` can be on the AMS units wired to
    ``active_extruder`` (an AMS-HT's one tray for slot 0), or None when the
    wiring is unknown.

    An AMS on the Filament Track Switch has no entry in the map (it can feed
    either nozzle), so one without an entry makes the answer unknown too.
    """
    if active_extruder is None or not ams_extruder_map:
        return None
    trays: set[int] = set()
    for ams_id, is_ht in ams_layout:
        nozzle = ams_extruder_map.get(str(ams_id))
        if nozzle is None:
            return None
        if nozzle != active_extruder:
            continue
        if not is_ht:
            trays.add(ams_id * 4 + raw_slot)
        elif raw_slot == 0:
            trays.add(ams_id)
    return trays


def resolve_expected_tray(
    raw_slot: int | None,
    ams_layout: list[tuple[int, bool]],
    mapping_raw: object,
    active_extruder: int | None = None,
    ams_extruder_map: dict | None = None,
) -> int | None:
    """Globalise a raw firmware ``tray_tar``/``tray_pre`` value for the runout UI (#2587).

    The firmware reports the target/previous slot as a bare number whose meaning
    depends on the AMS layout (see ``PrinterState.tray_tar``). This mirrors the
    ``tray_now`` handling so the resolved ID lines up with what the AMS graphic
    already highlights via ``ams_id*4 + slot``.

    ``ams_layout`` is a list of ``(ams_id, is_ams_ht)`` for the connected units.

    - ``255``/``-1`` (none/idle) -> ``None``
    - ``254`` (external spool) -> ``254``
    - ``128``-``135`` (AMS-HT) -> already global, returned as-is
    - ``0``-``3`` local slot:
        * exactly one regular AMS -> ``ams_id*4 + slot``
        * several regular AMS -> resolved via the snow-encoded ``mapping`` field
          (each entry = ``ams_hw_id*256 + slot``; ``65535`` = unmapped), then
          narrowed to the AMS that feed ``active_extruder`` (``ams_extruder_map``,
          dual-nozzle printers), or ``None`` when it stays ambiguous (honest
          "can't determine")
        * no regular AMS -> ``None``
    - ``4``-``15`` -> already a global regular-AMS ID, returned as-is

    Returns ``None`` for anything it can't place, so the caller surfaces a
    "check the printer" message instead of pointing at the wrong slot.
    """
    if raw_slot is None or raw_slot in (255, -1):
        return None
    if raw_slot == 254:
        return 254
    if 128 <= raw_slot <= 135:
        return raw_slot
    if 0 <= raw_slot <= 3:
        regular = [ams_id for ams_id, is_ht in ams_layout if not is_ht]
        if len(regular) == 1:
            return regular[0] * 4 + raw_slot
        if len(regular) > 1:
            candidates: set[int] = set()
            for value in mapping_raw if isinstance(mapping_raw, list) else []:
                if not isinstance(value, int) or value >= 65535:
                    continue
                ams_hw_id = value >> 8
                slot = value & 0xFF
                if 0 <= ams_hw_id <= 3 and (slot & 0x03) == raw_slot:
                    candidates.add(ams_hw_id * 4 + raw_slot)
                elif 128 <= ams_hw_id <= 135 and raw_slot == 0:
                    candidates.add(ams_hw_id)
            if len(candidates) == 1:
                return candidates.pop()
            # The print paused on the active nozzle, so the slot is on an AMS
            # that feeds it.
            narrowed = _trays_on_extruder(raw_slot, ams_layout, active_extruder, ams_extruder_map)
            if narrowed is None:
                return None
            if candidates:
                narrowed &= candidates
            return narrowed.pop() if len(narrowed) == 1 else None
        return None
    if 4 <= raw_slot <= 15:
        return raw_slot
    # 24-27 = A2L AMS-Lite (normalised unit 6) global tray ids, already resolved.
    if 24 <= raw_slot <= 27:
        return raw_slot
    return None


def _ams_temp(raw: object) -> float | None:
    """The AMS unit temperature as a number, or None when it is not one."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # NaN or infinity would put a token in the WebSocket frame that the
    # browser's JSON.parse rejects, dropping the whole status push.
    return value if math.isfinite(value) else None


def ams_units_to_dicts(
    ams_list: list,
    resolve_slot_k: Callable,
    drying_targets: dict[int, dict] | None = None,
) -> list[dict]:
    """The AMS units of a printer's ``raw_data["ams"]``, as the printer card reads them.

    The one serialiser for both the WebSocket push and the REST status
    (routes/printers.py). The card is seeded from REST and shallow-merged with
    every push, so any field one of them lacks reads as missing on the page:
    ``exists`` (#2670), the stalled-countdown flag and the drying status were
    each lost that way while the two were separate copies.
    """
    ams_units: list[dict] = []
    for ams_data in ams_list:
        # Skip if ams_data is not a dict (defensive check)
        if not isinstance(ams_data, dict):
            continue
        ams_id = int(ams_data.get("id", 0))
        trays = []
        for tray in ams_data.get("tray", []):
            tag_uid = tray.get("tag_uid")
            if tag_uid in ("", "0000000000000000"):
                tag_uid = None
            tray_uuid = tray.get("tray_uuid")
            if tray_uuid in ("", "00000000000000000000000000000000"):
                tray_uuid = None

            # Get K value: first try tray's k field, then lookup from K-profiles
            k_value = tray.get("k")
            cali_idx = tray.get("cali_idx")
            if k_value is None:
                k_value = resolve_slot_k(cali_idx, ams_id, int(tray.get("id", 0)))

            # P1S / A1 Mini physically-empty-slot signal (#1322 follow-up by
            # @RosdasHH): for a truly empty slot the firmware sends only
            # {"id": N} — no state, no tray_type, no anything else. Treat
            # that as the firmware's "no spool" indicator (state=9) so the
            # assign-spool path in inventory.py can short-circuit a MQTT
            # publish the firmware would silently drop anyway. The
            # post-"Reset Slot" A1 Mini BMCU case sends a populated payload
            # (state=3, tray_type="") — different shape, doesn't match this
            # guard, still attempts the MQTT push per the #1322 fix.
            state_val = tray.get("state")
            if state_val is None and len(tray) == 1 and "id" in tray:
                state_val = 9

            trays.append(
                {
                    "id": int(tray.get("id", 0)),
                    "tray_color": tray.get("tray_color"),
                    "tray_type": tray.get("tray_type"),
                    "tray_sub_brands": tray.get("tray_sub_brands"),
                    "tray_id_name": tray.get("tray_id_name"),
                    "tray_info_idx": tray.get("tray_info_idx"),
                    "remain": tray.get("remain", 0),
                    "k": k_value,
                    "cali_idx": cali_idx,
                    "tag_uid": tag_uid,
                    "tray_uuid": tray_uuid,
                    "nozzle_temp_min": tray.get("nozzle_temp_min"),
                    "nozzle_temp_max": tray.get("nozzle_temp_max"),
                    "drying_temp": tray.get("drying_temp"),
                    "drying_time": tray.get("drying_time"),
                    "state": state_val,
                    # Firmware's authoritative presence bit (tray_exist_bits),
                    # set by apply_tray_exist_bits. Without it getEmptySlotKind
                    # falls back to the firmware-variant state 9/10 heuristic,
                    # wrong for AMS-HT in both directions (#2670).
                    "exists": tray.get("exists"),
                }
            )
        # Percentage only — the 1-5 index is inverted and must never stand
        # in for one (#3140). See utils/ams_humidity.
        humidity_pct = ams_humidity_percent(ams_data)
        humidity_value = int(round(humidity_pct)) if humidity_pct is not None else None

        # AMS-HT by its unit id: a regular AMS can report fewer than 4 trays.
        is_ams_ht = 128 <= ams_id <= 135

        # Active-cycle filament + target temperature for the badge.
        # Bambu does not echo the cycle's chosen filament/temp on the
        # per-tick AMS push, so prefer the cached target from the last
        # ``send_drying_command``. When we have no record (drying
        # started in a previous backend lifetime, or the cache was
        # never seeded), the loaded trays can still name the filament
        # if they agree — but never the temperature, which only the
        # cache knows. See uniform_tray_filament_hint.
        ams_id_int = int(ams_data.get("id", 0))
        target = (drying_targets or {}).get(ams_id_int)
        dry_target_temp: int | None = None
        dry_filament: str | None = None
        if target:
            temp_val = target.get("temp")
            fil_val = target.get("filament") or ""
            if temp_val is not None:
                try:
                    dry_target_temp = int(temp_val)
                except (TypeError, ValueError):
                    dry_target_temp = None
            if fil_val:
                dry_filament = str(fil_val)
        if not dry_filament:
            dry_filament = uniform_tray_filament_hint([tray.get("tray_type") or "" for tray in trays])

        ams_units.append(
            {
                "id": ams_id_int,
                "humidity": humidity_value,
                # A number, as the REST schema has always coerced it: the
                # firmware sends a string, and the card showed "24°C" after a
                # page load but "24.0°C" after the first push.
                "temp": _ams_temp(ams_data.get("temp")),
                "is_ams_ht": is_ams_ht,
                "tray": trays,
                # Serial number: Bambu MQTT uses "sn" key on AMS unit objects
                "serial_number": str(ams_data.get("sn") or ams_data.get("serial_number") or ""),
                # Firmware version: populated by _handle_version_info from get_version
                "sw_ver": str(ams_data.get("sw_ver") or ""),
                # Drying: dry_time > 0 means drying is active (minutes remaining)
                "dry_time": int(ams_data.get("dry_time") or 0),
                # Stall flag stamped onto raw_data by the MQTT layer: the
                # firmware set the countdown but it never ticked. Without it
                # the card falls back to the amber "active cycle" badge, so
                # the neutral "Drying not started" state is unreachable.
                "dry_countdown_stalled": bool(ams_data.get("dry_countdown_stalled") or False),
                # Drying status from info hex bits (0=Off, 1=Checking, 2=Drying, 3=Cooling, etc.)
                "dry_status": int(ams_data.get("dry_status") or 0),
                "dry_sub_status": int(ams_data.get("dry_sub_status") or 0),
                # Cannot-dry reasons from firmware (e.g. 1=InsufficientPower, 8=NeedPluginPower)
                "dry_sf_reason": list(ams_data.get("dry_sf_reason") or []),
                # Active-cycle filament name + target temperature
                "dry_target_temp": dry_target_temp,
                "dry_filament": dry_filament,
                # Module type: "ams", "n3f", "n3s" (from get_version)
                "module_type": str(ams_data.get("module_type") or ""),
            }
        )
    return ams_units


def vt_trays_to_dicts(raw: object, resolve_slot_k: Callable) -> list[dict]:
    """The external spools of a printer's ``raw_data["vt_tray"]``.

    Shared by the WebSocket push and the REST status, like ``ams_units_to_dicts``.
    """
    vt_tray: list[dict] = []
    vt_tray_raw = raw
    # Defensive: MQTT sends vt_tray as a dict; normalize to list
    if isinstance(vt_tray_raw, dict):
        vt_tray_raw = [vt_tray_raw]
    elif not isinstance(vt_tray_raw, list):
        vt_tray_raw = []
    for vt_data in vt_tray_raw:
        vt_tag_uid = vt_data.get("tag_uid")
        if vt_tag_uid in ("", "0000000000000000"):
            vt_tag_uid = None
        vt_tray_uuid = vt_data.get("tray_uuid")
        if vt_tray_uuid in ("", "00000000000000000000000000000000"):
            vt_tray_uuid = None

        # Get K value for vt_tray
        vt_k_value = vt_data.get("k")
        vt_cali_idx = vt_data.get("cali_idx")
        if vt_k_value is None:
            # External holder: id 254 is Ext-L, 255 is Ext-R. The resolver
            # takes the 0/1 tray index, so normalise before asking.
            vt_id = int(vt_data.get("id", 254))
            vt_k_value = resolve_slot_k(vt_cali_idx, 255, vt_id - 254 if vt_id >= 254 else vt_id)

        tray_id = int(vt_data.get("id", 254))
        vt_tray.append(
            {
                "id": tray_id,
                "tray_color": vt_data.get("tray_color"),
                "tray_type": vt_data.get("tray_type"),
                "tray_sub_brands": vt_data.get("tray_sub_brands"),
                "tray_id_name": vt_data.get("tray_id_name"),
                "tray_info_idx": vt_data.get("tray_info_idx"),
                "remain": vt_data.get("remain", 0),
                "k": vt_k_value,
                "cali_idx": vt_cali_idx,
                "tag_uid": vt_tag_uid,
                "tray_uuid": vt_tray_uuid,
                "nozzle_temp_min": vt_data.get("nozzle_temp_min"),
                "nozzle_temp_max": vt_data.get("nozzle_temp_max"),
            }
        )
    return vt_tray


def printer_state_to_dict(
    state: PrinterState,
    printer_id: int | None = None,
    model: str | None = None,
    drying_targets: dict[int, dict] | None = None,
) -> dict:
    """Convert PrinterState to a JSON-serializable dict.

    Args:
        state: The printer state to convert
        printer_id: Optional printer ID for generating cover URLs
        model: Optional printer model for filtering unsupported features
        drying_targets: Optional per-AMS active-cycle params
            (``{ams_id: {"filament": str, "temp": int}}``) sourced from the
            BambuMQTTClient cache so the badge can display "PETG @ 65°C".
    """
    from backend.app.services.printer_manager import printer_manager

    # Parse AMS data from raw_data
    ams_units = []
    vt_tray = []
    raw_data = state.raw_data or {}

    # K value for a slot's bound profile. Shared with the REST serializer of
    # the same card (routes/printers.py) so the two cannot answer differently:
    # this one used to key on cali_idx alone, which on a dual-nozzle machine
    # meant whichever nozzle's table was listed last won the slot.
    resolve_slot_k = build_slot_k_resolver(state)

    if "ams" in raw_data and isinstance(raw_data["ams"], list):
        ams_units = ams_units_to_dicts(raw_data["ams"], resolve_slot_k, drying_targets)

    # Parse virtual tray (external spool) — now a list
    if "vt_tray" in raw_data:
        vt_tray = vt_trays_to_dicts(raw_data["vt_tray"], resolve_slot_k)

    # Get ams_extruder_map from raw_data (populated by MQTT handler from AMS info field)
    ams_extruder_map = (
        state.ams_extruder_map
        if isinstance(state.ams_extruder_map, dict) and state.ams_extruder_map
        else raw_data.get("ams_extruder_map", {})
    )

    # Filter out chamber temp for models that don't have a real sensor
    # P1P, P1S, A1, A1Mini report meaningless chamber_temper values
    temperatures = state.temperatures
    if not supports_chamber_temp(model):
        temperatures = {
            k: v for k, v in temperatures.items() if k not in ("chamber", "chamber_target", "chamber_heating")
        }

    result = {
        "sdcard": bool(getattr(state, "sdcard", False)),
        "store_to_sdcard": bool(getattr(state, "store_to_sdcard", False)),
        "timelapse": bool(getattr(state, "timelapse", False)),
        "ipcam": bool(getattr(state, "ipcam", False)),
        "print_options": asdict(state.print_options) if is_dataclass(getattr(state, "print_options", None)) else None,
        "stg": list(getattr(state, "stg", []) or []),
        "mc_print_sub_stage": getattr(state, "mc_print_sub_stage", 0),
        "last_ams_update": getattr(state, "last_ams_update", 0.0),
        "supports_chamber_heater": supports_chamber_heater(model),
        "ams_exists": "ams" in raw_data and isinstance(raw_data["ams"], list),
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
        "hms_errors": [
            {
                "code": e.code,
                "attr": e.attr,
                "module": e.module,
                "severity": e.severity,
                "actions": e.actions,
                "job_id": e.job_id,
                "full_code": e.full_code,
                # Same field as the status response carries (#2926) — a relay
                # watching the stream should not have to poll REST to find out
                # what a fault means.
                "description": e.description,
            }
            for e in (state.hms_errors or [])
        ],
        # AMS data for filament colors
        "ams": ams_units if ams_units else None,
        "vt_tray": vt_tray,
        # AMS status for filament change tracking
        "ams_status_main": state.ams_status_main,
        "ams_status_sub": state.ams_status_sub,
        "tray_now": state.tray_now,
        # Grows only when tray_now changes, which already triggers a push.
        "tray_change_log": [[tray, layer] for tray, layer in getattr(state, "tray_change_log", None) or []],
        # Runout / filament-replacement guidance (#2587). Only meaningful while
        # PAUSED — resolve the firmware's target/previous slot to a global tray ID
        # so the AMS graphic can highlight the slot the print now expects and name
        # the one that ran out. None when idle, not paused, or unresolvable.
        "expected_tray": (
            resolve_expected_tray(
                state.tray_tar,
                [(u["id"], u.get("is_ams_ht", False)) for u in ams_units],
                raw_data.get("mapping"),
                active_extruder=state.active_extruder,
                ams_extruder_map=nozzle_wiring(state),
            )
            if state.state == "PAUSE"
            else None
        ),
        "previous_tray": (
            resolve_expected_tray(
                state.tray_pre,
                [(u["id"], u.get("is_ams_ht", False)) for u in ams_units],
                raw_data.get("mapping"),
                active_extruder=state.active_extruder,
                ams_extruder_map=nozzle_wiring(state),
            )
            if state.state == "PAUSE"
            else None
        ),
        # Per-AMS extruder map: {ams_id: extruder_id} where 0=right, 1=left
        "ams_extruder_map": ams_extruder_map,
        # The running print's mapping, for the slot pills (#1762).
        "ams_mapping": print_ams_mapping(state),
        # Pushed so a change reaches the card at once rather than with the
        # 30 s REST poll; the frontend shallow-merges each push over its status.
        "nozzles": [
            {"nozzle_type": n.nozzle_type, "nozzle_diameter": n.nozzle_diameter} for n in (state.nozzles or [])
        ],
        "firmware_version": state.firmware_version,
        "developer_mode": state.developer_mode,
        # Filament Track Switch. Both fields have to travel on the WebSocket, not
        # only on the REST status: the frontend shallow-merges each push over its
        # cached status, so a field that is absent here keeps whatever the last
        # full fetch left behind. Omitting them meant the AMS inlet badges only
        # ever changed on a page reload.
        "fila_switch": (
            {
                "installed": True,
                "in_slots": list(state.fila_switch.in_slots),
                "out_extruders": list(state.fila_switch.out_extruders),
                "stat": state.fila_switch.stat,
                "info": state.fila_switch.info,
                # Mirrors BambuStudio's DevFilaSwitch::IsReady — every AMS has to
                # be bound to an inlet before the switch can route anything. Until
                # the operator has done that on the printer's Manual AMS Setup
                # screen, Studio refuses a load outright rather than sending a
                # command the firmware cannot act on, and so do we.
                # An empty AMS list is "ready", as it is in Studio: there is then
                # no slot to load from, so nothing can reach the check anyway, and
                # reporting not-ready would only mean a confusing toast on a
                # payload that has not carried the AMS block yet.
                #
                # An AMS still reporting a real extruder id rather than 0xE has no
                # inlet entry, so a machine with one hard-wired unit reads as not
                # ready. That looks harsh but is exactly Studio's own rule —
                # IsReady() requires a switcher position on *every* AMS, and only
                # the 0xE branch ever sets one (DevFilaSystem.cpp:596-615).
                "ready": all(str(u["id"]) in state.ams_switch_inlet for u in ams_units),
            }
            if state.fila_switch and state.fila_switch.installed
            else None
        ),
        # Per-AMS FTS inlet binding: {ams_id: "A" | "B"}. Gated on the accessory
        # so a stale binding cannot outlive it being unplugged.
        "ams_switch_inlet": (dict(state.ams_switch_inlet) if state.fila_switch and state.fila_switch.installed else {}),
        # Which AMS slot each hotend is fed from: {extruder_id: {...}}. Travels on
        # the WebSocket for the same reason as fila_switch above — the frontend
        # shallow-merges pushes over its cached status, so an absent field keeps a
        # stale value forever. Empty on printers that do not report it.
        "extruder_slots": {
            str(ext_id): {
                "ams_id": slot.ams_id,
                "slot_id": slot.slot_id,
                "has_filament": slot.has_filament,
            }
            for ext_id, slot in state.extruder_slots.items()
        },
        # WiFi signal strength
        "wifi_signal": state.wifi_signal,
        "wired_network": state.wired_network,
        "door_open": state.door_open,
        # AMS Filament Backup state (auto-switch to second spool). Tri-state:
        # True / False / None. None = unknown or unsupported (A1 family). UI
        # uses this to drive the small status icon next to the AMS drying icon.
        "ams_filament_backup": state.ams_filament_backup,
        # Calibration stage tracking
        "stg_cur": state.stg_cur,
        "stg_cur_name": get_derived_status_name(state, model),
        # Printable objects count for skip objects feature
        "printable_objects_count": len(state.printable_objects),
        # The next pause the slicer put into the running print (#2984)
        "next_pause": next_pause(state),
        # Fan speeds (0-100 percentage, None if not available)
        "cooling_fan_speed": state.cooling_fan_speed,
        "big_fan1_speed": state.big_fan1_speed,
        "big_fan2_speed": state.big_fan2_speed,
        "heatbreak_fan_speed": state.heatbreak_fan_speed,
        "left_aux_fan_speed": state.left_aux_fan_speed,
        "exhaust_fan_present": state.exhaust_fan_present,
        # Chamber light state
        "chamber_light": state.chamber_light,
        # Active extruder for dual-nozzle printers (0=right, 1=left)
        "active_extruder": state.active_extruder,
        # Print speed mode (1=silent, 2=standard, 3=sport, 4=ludicrous)
        "speed_level": state.speed_level,
        # Switched on the printer's screen too; REST-only, the card showed the
        # old mode until the next poll (PSF-4).
        "airduct_mode": state.airduct_mode,
        # H2C nozzle rack (tool-changer dock positions)
        # Map raw MQTT field names (type/diameter) to schema names (nozzle_type/nozzle_diameter)
        "nozzle_rack": [
            {
                "id": n.get("id", 0),
                "nozzle_type": n.get("type", ""),
                "nozzle_diameter": n.get("diameter", ""),
                "wear": n.get("wear"),
                "stat": n.get("stat"),
                "max_temp": n.get("max_temp", 0),
                "serial_number": n.get("serial_number", ""),
                "filament_color": n.get("filament_color", ""),
                "filament_id": n.get("filament_id", ""),
                # The push replaces the whole rack: without it a slot lost its
                # material label on every push (PSF-3).
                "filament_type": n.get("filament_type", ""),
            }
            for n in (state.nozzle_rack or [])
        ],
        # AMS drying support
        "supports_drying": supports_drying(model, state.firmware_version),
        "supports_drying_while_printing": supports_drying_while_printing(model, state.firmware_version),
        "drying_screen_only": drying_screen_only(model),
        # 1-indexed plate number parsed from gcode_file (e.g. /Metadata/plate_2.gcode).
        # Pushed via WebSocket so the printer card picks up plate transitions within
        # a multi-plate 3MF without waiting for the 30 s REST poll (#881 follow-up).
        # current_archive_id is intentionally REST-only — it's stable for the life
        # of a print and needs a DB lookup the WebSocket path shouldn't pay for.
        "current_plate_id": resolve_plate_id(state),
        # Plate-clear gate (#939). Lives on the PrinterManager rather than PrinterState,
        # so surface it here — without this, WebSocket merges drop the flag and the
        # "Clear Plate" button only appears when the 30 s REST fallback poll runs.
        "awaiting_plate_clear": printer_manager.is_awaiting_plate_clear(printer_id) if printer_id else False,
    }
    # Add cover URL if there's an active print and printer_id is provided
    # Include PAUSE state so skip objects modal can show cover
    if printer_id and state.state in ("RUNNING", "PAUSE") and state.gcode_file:
        result["cover_url"] = f"/api/v1/printers/{printer_id}/cover"
    else:
        result["cover_url"] = None
    # The display name + model, so a WS consumer can label the printer from the
    # snapshot alone (#963 follow-up; the SPA itself takes them from
    # /api/v1/printers). PrinterInfo only carries name/serial_number; the model
    # comes through via the `model` arg.
    if printer_id:
        _printer_info = printer_manager.get_printer(printer_id)
        if _printer_info is not None:
            result["name"] = _printer_info.name
    if model:
        result["model"] = model
    return result

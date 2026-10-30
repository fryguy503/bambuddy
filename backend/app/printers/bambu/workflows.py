"""Bambu protocol interpretation used by core archive and event workflows.

The application owns persistence; this replaceable package owns wire meaning.
"""

import re

_HMS_FAILURE_REASONS: dict[str, str] = {
    # Layer shift / step loss
    "0300_4057": "layerShift",
    "0300_4068": "layerShift",
    "0300_800C": "layerShift",
    # Filament runout (printer-side & per-AMS-slot)
    "0300_8004": "filamentRunout",
    "0700_8011": "filamentRunout",
    "0701_8011": "filamentRunout",
    "0702_8011": "filamentRunout",
    "0703_8011": "filamentRunout",
    "0704_8011": "filamentRunout",
    "0705_8011": "filamentRunout",
    "0706_8011": "filamentRunout",
    "0707_8011": "filamentRunout",
    "07FF_8011": "filamentRunout",
    # Clogged nozzle / extruder
    "0300_4006": "cloggedNozzle",
    "0300_8016": "cloggedNozzle",
    "0300_801C": "cloggedNozzle",
    "0700_8003": "cloggedNozzle",
    "0700_8007": "cloggedNozzle",
    "0700_8013": "cloggedNozzle",
    "0701_8003": "cloggedNozzle",
    "0701_8007": "cloggedNozzle",
    "0701_8013": "cloggedNozzle",
    "0702_8003": "cloggedNozzle",
    # AI print monitoring — spaghetti / the model coming off the plate.
    # `spaghettiDetached` is a key the archive editor already offers for this
    # failure mode, not a new one, so a derived reason opens the dropdown on
    # that option rather than blank — and survives the next save, which clears
    # any value the editor does not recognise.
    #
    # A module-0x0C row is safe here despite the warning above: that warning is
    # about matching on the module alone, and 0C00_8042 is a full short code
    # with a documented meaning ("The AI print monitor has detected a spaghetti
    # defect", hms_errors.py). The H2D cancel echo is 0C00_001B, so the two
    # cannot collide.
    #
    # 0300_8003's own text ends "before continuing your print", but on an X2D
    # it arrives with the print already paused, offering only
    # RESUME_PRINTING_DEFECTS / STOP_PRINTING — a halt waiting on the user.
    #
    # Two neighbours are left out on purpose, so this does not get re-derived:
    #   * 0C00_C004 "Possible spaghetti failure was detected." — "possible"
    #     reads as a warning about a print that is still running, not a halt.
    #   * 0300_800A is AI monitoring too, but it reports a filament pile-up in
    #     the waste chute. That is not the print failing.
    #
    # That line is drawn from the text, not from `severity`, because severity
    # cannot draw it: 0300_8003 reaches us through `print_error`, a bare
    # module/error word with no level in it, and bambu_mqtt.py gives every
    # print_error entry a flat severity=3. Matching on the short code alone is
    # the right shape for derive_failure_reason, not an omission.
    "0300_8003": "spaghettiDetached",
    "0C00_8042": "spaghettiDetached",
}


def _hms_short_code(attr: int, code: int | str) -> str:
    """Build the canonical "MMMM_CCCC" HMS short code from raw attr/code values."""
    if isinstance(code, str):
        code_int = int(code.replace("0x", ""), 16) if code else 0
    else:
        code_int = int(code or 0)
    attr_int = int(attr or 0)
    return f"{(attr_int >> 16) & 0xFFFF:04X}_{code_int & 0xFFFF:04X}"


def derive_failure_reason(status: str, hms_errors: list[dict] | None) -> str | None:
    """Derive the failure_reason key for an archived print.

    Returns "userCancelled" for cancelled/aborted prints; for failed prints,
    returns the first matching key from _HMS_FAILURE_REASONS, or None when
    no HMS code matches (don't guess — null is honest). The keys are the
    archive editor's vocabulary (_FAILURE_REASON_KEYS in print_log.py) and are
    translated at render time.
    """
    if status in ("aborted", "cancelled"):
        return "userCancelled"
    if status != "failed":
        return None
    for err in hms_errors or []:
        short_code = _hms_short_code(err.get("attr", 0), err.get("code", 0))
        if short_code in _HMS_FAILURE_REASONS:
            return _HMS_FAILURE_REASONS[short_code]
    return None


def _extract_filament_data_from_mqtt(data: dict, ams_mapping: list[int] | None = None) -> dict[str, str]:
    """Best-effort filament metadata from the MQTT print-start snapshot.

    Used when the 3MF can't be downloaded (P1S/A1/P2S firmwares lock the
    file during print, see #1533) so the fallback PrintArchive still has
    enough filament info to support the inventory views and AMS-expansion
    planning the operator opens it for. Returns a dict with optional
    ``filament_type`` and ``filament_color`` keys in the same
    comma-separated format the 3MF extractor produces, so the rest of the
    codebase treats the fallback archive identically to a normal one.

    ``ams_mapping`` is the slicer's slot-per-print-filament list captured
    from the MQTT print payload (global tray IDs, possibly -1 for VT-tray
    entries). When supplied, only the slots actually consumed by this
    print contribute. Without it the function falls back to every loaded
    AMS slot — less accurate but still useful.

    Accepts both the raw inner payload (``{"ams": {"ams": [...]}, ...}``)
    that the unit tests pass directly, AND the on_print_start callback
    shape (``{"raw_data": {"ams": {"ams": [...]}, ...}, ...}``) the
    bambu_mqtt service hands to main.py at runtime. The original
    ``_extract_filament_data_from_mqtt(data)`` shipped in #1533 only
    handled the inner shape and silently returned ``{}`` for every real
    print start, leaving fallback archives' filament fields NULL — the
    exact regression the fix was meant to close. Reported with a log
    proving the AMS state was right there at
    ``data["raw_data"]["ams"]["ams"][0]["tray"][0]`` (#1533 follow-up).
    """
    result: dict[str, str] = {}
    # Look at the on_print_start wrapper first, then the inner shape.
    raw_data = (data or {}).get("raw_data")
    ams_root = (raw_data or {}).get("ams") if isinstance(raw_data, dict) else None
    if not isinstance(ams_root, dict):
        ams_root = (data or {}).get("ams") or {}
    ams_units = ams_root.get("ams") if isinstance(ams_root, dict) else None
    if not isinstance(ams_units, list) or not ams_units:
        return result

    from .materials import global_tray_id as _global_tray_id

    # Map global tray id → (type, color), addressed as the print's ams_mapping
    # is: unit * 4 + tray, or the bare unit id for an AMS-HT (from 128).
    loaded: dict[int, tuple[str, str]] = {}
    for unit in ams_units:
        if not isinstance(unit, dict):
            continue
        try:
            unit_id = int(unit.get("id", 0))
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            try:
                tray_id = int(tray.get("id", 0))
            except (TypeError, ValueError):
                continue
            ttype = (tray.get("tray_type") or "").strip()
            # RRGGBBAA from the printer, stored as the 3MF path stores it:
            # "#RRGGBB" (the Archives page's swatches and colour filter).
            hex_color = (tray.get("tray_color") or "").strip().upper()[:6]
            tcolor = f"#{hex_color}" if re.fullmatch(r"[0-9A-F]{6}", hex_color) else ""
            if not ttype:
                continue  # Empty / unloaded slot.
            loaded[_global_tray_id(unit_id, tray_id)] = (ttype, tcolor)

    if not loaded:
        return result

    if ams_mapping:
        used_ids = [int(x) for x in ams_mapping if isinstance(x, (int, float)) and int(x) >= 0]
        filaments = [loaded[g] for g in used_ids if g in loaded]
        if not filaments:
            return result  # Mapping points entirely at slots we have no data for.
    else:
        filaments = [loaded[g] for g in sorted(loaded.keys())]

    types_joined = ",".join(f[0] for f in filaments)
    colors_joined = ",".join(f[1] for f in filaments if f[1])

    # Column limits per backend/app/models/archive.py: filament_type=50,
    # filament_color=200.
    if types_joined:
        result["filament_type"] = types_joined[:50]
    if colors_joined:
        result["filament_color"] = colors_joined[:200]
    return result


def _is_bambu_uuid(tray_uuid: str) -> bool:
    """Check if a tray UUID looks like a valid Bambu Lab RFID UUID (non-empty, non-zero)."""
    return bool(tray_uuid) and tray_uuid not in ("", "0" * len(tray_uuid))


async def on_fts_inlet_change(printer_id: int, ams_id: int, inlet: str):
    """Re-point a moved AMS's K-profiles at the nozzle it now feeds.

    K-profiles are per-nozzle and the printer's calibration table is numbered
    per-nozzle, but a tray holds exactly one ``cali_idx``. Moving an AMS to the
    switch's other inlet therefore silently invalidates every configured slot in
    it: the index stays put and now resolves against the other nozzle's table.
    Measured on the maintainer's H2C — one spool calibrated 0.018 on the left
    and 0.020 on the right kept the left profile after the move, and a manual
    RFID re-read only re-asserted the same wrong one.

    Configuring a slot is a deliberate preparation step, so this re-selects
    rather than re-configures: only the calibration binding moves, and only for
    slots whose spool already has a stored profile for the new nozzle. A slot
    Bambuddy knows nothing about is left exactly as the operator left it.
    """
    from backend.app import main as host

    logger = host.logging.getLogger(__name__)
    target_extruder = host.extruder_for_inlet(inlet)
    if target_extruder is None:
        return
    client = host.printer_manager.get_client(printer_id)
    state = host.printer_manager.get_status(printer_id)
    if not client or not state or (not state.raw_data):
        return
    nozzle_diameter = host.nozzle_diameter_for_extruder(
        state, target_extruder, host.printer_manager.get_model(printer_id)
    )
    ams_raw = state.raw_data.get("ams")
    ams_list = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
    unit = next((u for u in ams_list if str(u.get("id")) == str(ams_id)), None)
    if not unit:
        return
    try:
        async with host.async_session() as db:
            for tray in unit.get("tray", []):
                tray_id = int(tray.get("id", -1))
                if tray_id < 0 or not tray.get("tray_type"):
                    continue
                current_idx = tray.get("cali_idx")
                profile = await host.find_slot_kprofile_for_extruder(
                    db,
                    printer_id,
                    ams_id,
                    tray_id,
                    target_extruder,
                    nozzle_diameter,
                    host.printer_manager.get_model(printer_id),
                    host.nozzle_flow_for_extruder(state, target_extruder, host.printer_manager.get_model(printer_id)),
                )
                if profile is None or profile.cali_idx is None:
                    continue
                if current_idx == profile.cali_idx:
                    continue
                logger.info(
                    "[Printer %s] AMS %s slot %s moved to inlet %s (nozzle %s): re-selecting K-profile %s (cali_idx %s -> %s, K=%s)",
                    printer_id,
                    ams_id,
                    tray_id,
                    inlet,
                    target_extruder,
                    profile.name,
                    current_idx,
                    profile.cali_idx,
                    profile.k_value,
                )
                client.extrusion_cali_sel(
                    ams_id=ams_id,
                    tray_id=tray_id,
                    cali_idx=profile.cali_idx,
                    filament_id=host.printer_safe_filament_id(profile.filament_id, tray.get("tray_info_idx", "")),
                    nozzle_diameter=nozzle_diameter,
                )
    except Exception as e:
        logger.warning("[Printer %s] Could not re-apply K-profiles after inlet move: %s", printer_id, e)


def status_fingerprint(state):
    telemetry = state.raw_data or {}
    temps = state.temperatures or {}
    nozzle_temp = round(temps.get("nozzle", 0))
    bed_temp = round(temps.get("bed", 0))
    nozzle_2_temp = round(temps.get("nozzle_2", 0)) if "nozzle_2" in temps else ""
    chamber_temp = round(temps.get("chamber", 0)) if "chamber" in temps else ""
    # Include target temps for heating phase detection
    bed_target = round(temps.get("bed_target", 0))
    nozzle_target = round(temps.get("nozzle_target", 0))

    # Include tray_now and vt_tray hash so external spool changes trigger broadcasts
    vt_tray_key = hash(str(telemetry.get("vt_tray", []))) if telemetry else 0
    # Include AMS dry_time and tray state values so drying/slot changes trigger broadcasts.
    #
    # dry_countdown_stalled rides along because it is the one drying signal the
    # countdown itself cannot carry: the MQTT layer raises it precisely BECAUSE
    # dry_time stopped moving, so on the frame that flips it every other member
    # of this key is identical and the push would be deduplicated away. Mid-print
    # a temperature would eventually break the tie, but a parked command on an
    # idle machine changes nothing else at all — AMS temp and humidity are not in
    # the key — so the badge could sit unreachable indefinitely. The flag flips at
    # most once per drying cycle, so it costs no mid-print broadcast traffic.
    ams_dry_key = (
        tuple((a.get("dry_time", 0), bool(a.get("dry_countdown_stalled"))) for a in (telemetry.get("ams") or []))
        if telemetry
        else ()
    )
    # Include tray states so load/unload transitions (state 11→10) trigger broadcasts (#784)
    #
    # The filament identity fields are here because Configure Slot writes
    # exactly those and nothing else. Re-configuring a slot from PLA to another
    # brand or colour of PLA leaves id/tray_type/state identical, so the key
    # matched, this function returned before broadcasting, and the card kept
    # showing the old filament until the 30s fallback poll or a page reload —
    # even though the configure route asks the printer for a fresh pushall and
    # that push does carry the new values. Reset always worked, because it
    # clears tray_type.
    #
    # These fields only change when someone configures a slot or swaps a spool,
    # so unlike temperature or progress they add no broadcast traffic mid-print.
    ams_tray_key = (
        tuple(
            (
                t.get("id"),
                t.get("tray_type", ""),
                t.get("state"),
                t.get("tray_color", ""),
                t.get("tray_info_idx", ""),
                t.get("tray_sub_brands", ""),
                t.get("cali_idx"),
            )
            for a in (telemetry.get("ams") or [])
            for t in a.get("tray", [])
        )
        if telemetry
        else ()
    )
    # Filament Track Switch: which inlet each AMS is bound to, and whether the
    # accessory is fitted at all. Neither is in ams_tray_key (it is per-tray) nor
    # in the AMS change-hash (tray fields only, and widening that would fire
    # spurious Spoolman syncs), so without them a "Join IN-B" on the printer
    # screen changed no key at all and the card's inlet badges sat stale until a
    # reload. Like the filament-backup flag, these only move when someone
    # reconfigures the machine, so they add no mid-print broadcast traffic.
    fts_key = (
        state.fila_switch.installed if state.fila_switch else False,
        tuple(sorted(state.ams_switch_inlet.items())),
        # Which hotend holds which slot. Unlike the two above this does move
        # mid-print, on every filament change — but only between discrete slots,
        # so it adds a push per toolchange, not a stream. The AMS slot menu needs
        # it live: it decides which hotend the Load dialog may offer and whether
        # Unload has anything to act on.
        tuple(
            sorted(
                ((ext, slot.ams_id, slot.slot_id, slot.has_filament) for ext, slot in state.extruder_slots.items()),
                # Sort on the extruder id alone: the other members are nullable
                # and comparing None with an int raises.
                key=lambda entry: entry[0],
            )
        ),
    )
    status_key = (
        f"{state.connected}:{state.state}:{state.progress}:{state.layer_num}:"
        f"{nozzle_temp}:{bed_temp}:{nozzle_2_temp}:{chamber_temp}:"
        f"{state.stg_cur}:{bed_target}:{nozzle_target}:"
        f"{state.cooling_fan_speed}:{state.big_fan1_speed}:{state.big_fan2_speed}:"
        f"{state.chamber_light}:{state.active_extruder}:{state.tray_now}:{vt_tray_key}:"
        f"{ams_dry_key}:{ams_tray_key}:{state.door_open}:{state.ams_filament_backup}:{fts_key}"
    )

    # Fields that change only when the machine does (a nozzle swap, a firmware
    # update, developer mode) or once per print (its AMS mapping): no traffic,
    # but on an idle printer nothing else moves the key when they change.
    machine_key = (
        tuple((n.nozzle_type, n.nozzle_diameter) for n in (state.nozzles or [])),
        state.firmware_version,
        state.developer_mode,
        tuple(state.print_ams_mapping) if isinstance(state.print_ams_mapping, list) else None,
        # Cooling/heating, switched on the printer's screen too: on an idle
        # printer nothing else moves the key (PSF-4).
        state.airduct_mode,
    )
    return f"{status_key}:{machine_key}"


async def _unlink_stale_assignments(printer_id: int, ams_data: list, printing_now: bool) -> None:
    """Unlink built-in inventory assignments whose slot no longer holds their spool.

    Runs from ``on_ams_change`` and, for removals held by ``slot_unlink_grace``,
    from the delayed re-check -- which is why it takes the AMS data and print
    state as arguments instead of reading them from the push (#3186).
    """
    from backend.app import main as host

    logger = host.logging.getLogger(__name__)

    from backend.app.utils.color_utils import colors_similar as _colors_similar

    # Auto-unlink spool assignments with stale fingerprints. Under the
    # per-printer assignment lock since #3186: the held-removal re-check runs
    # this outside any MQTT push, so it can now overlap one.
    try:
        async with host.get_ams_assignment_lock(printer_id), host.async_session() as db:
            from sqlalchemy.orm import selectinload

            from backend.app.api.routes.inventory import _find_tray_in_ams_data
            from backend.app.models.spool import Spool as _Spool
            from backend.app.models.spool_assignment import SpoolAssignment as SA
            from backend.app.services.ams_slot_presence import spool_present
            from backend.app.services.inventory_mode import spoolman_owns_assignments

            # Built-in assignments only. Since #2812 they survive a switch to
            # Spoolman mode rather than being deleted by it, and this pass ends
            # in ``db.delete`` — left ungated it would unlink them one slot at a
            # time as the AMS contents changed under the other mode, undoing the
            # preservation more slowly but just as completely.
            assignments = []
            if not await spoolman_owns_assignments(db):
                result = await db.execute(
                    host.select(SA)
                    .where(SA.printer_id == printer_id)
                    .options(selectinload(SA.spool).selectinload(_Spool.k_profiles))
                )
                assignments = result.scalars().all()
            # ``printing_now`` (top of this function) keeps a runout from
            # unlinking the spool that fed the print — the next idle-time pass
            # unlinks it if the user really did take it out.
            stale = []
            # Removals this pass is holding rather than unlinking (#3186).
            held: set[tuple] = set()
            for assignment in assignments:
                # External spool assignments (ams_id=255) live in vt_tray, not AMS data
                if assignment.ams_id == 255:
                    ps = host.printer_manager.get_status(printer_id)
                    vt_tray_raw = ps.raw_data.get("vt_tray", []) if ps else []
                    ext_id = assignment.tray_id + 254  # 0→254, 1→255
                    current_tray = None
                    for vt in vt_tray_raw:
                        if isinstance(vt, dict) and int(vt.get("id", 254)) == ext_id:
                            current_tray = vt
                            break
                    if not current_tray:
                        # vt_tray data may not have arrived yet — keep assignment
                        continue
                else:
                    current_tray = _find_tray_in_ams_data(ams_data, assignment.ams_id, assignment.tray_id)
                if not current_tray:
                    if printing_now:
                        logger.info(
                            "Auto-unlink skipped: spool %d AMS%d-T%d — slot empty during a running print (runout?)",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                        )
                        continue
                    # A whole AMS unit can drop out of one push and come back in
                    # the next; only a slot that stays gone is a removal (#3186).
                    hold_key = ("inventory", assignment.ams_id, assignment.tray_id, assignment.spool_id)
                    if not host.slot_unlink_grace.removal_confirmed(printer_id, hold_key):
                        held.add(hold_key)
                        logger.info(
                            "Auto-unlink held: spool %d AMS%d-T%d — tray not found in AMS data; "
                            "unlinking if it is still gone in %ds",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                            int(host.slot_unlink_grace.GRACE_SECONDS),
                        )
                        continue
                    logger.info(
                        "Auto-unlink: spool %d AMS%d-T%d — tray not found in AMS data (slot empty?)",
                        assignment.spool_id,
                        assignment.ams_id,
                        assignment.tray_id,
                    )
                    stale.append(assignment)  # Slot empty
                elif host._is_bambu_uuid(current_tray.get("tray_uuid", "")):
                    # A Bambu Lab spool is in this slot — check if it's the same spool
                    # that's currently assigned. If yes, keep the assignment (avoids
                    # unnecessary unlink/re-assign/ams_filament_setting cycle that clears
                    # the printer's filament preset on every startup).
                    tray_uuid = current_tray.get("tray_uuid", "")
                    tag_uid = current_tray.get("tag_uid", "")
                    spool = assignment.spool
                    spool_matches = False
                    if spool:
                        if (spool.tray_uuid and spool.tray_uuid.upper() == tray_uuid.upper()) or (
                            spool.tag_uid
                            and tag_uid
                            and tag_uid != "0000000000000000"
                            and spool.tag_uid.upper() == tag_uid.upper()
                        ):
                            spool_matches = True
                    if spool_matches:
                        # Same BL spool still in slot — keep assignment, update fingerprint if needed
                        cur_color = current_tray.get("tray_color", "")
                        cur_type = current_tray.get("tray_type", "")
                        fp_color = assignment.fingerprint_color or ""
                        fp_type = assignment.fingerprint_type or ""
                        if cur_color.upper() != fp_color.upper() or cur_type.upper() != fp_type.upper():
                            assignment.fingerprint_color = cur_color
                            assignment.fingerprint_type = cur_type
                            logger.debug(
                                "Auto-unlink: spool %d AMS%d-T%d — same BL spool, updated fingerprint",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                        continue
                    # Different BL spool or unrecognized — unlink so auto-assign can match
                    logger.info(
                        "Auto-unlink: spool %d AMS%d-T%d — different Bambu Lab spool detected (uuid=%s)",
                        assignment.spool_id,
                        assignment.ams_id,
                        assignment.tray_id,
                        tray_uuid,
                    )
                    stale.append(assignment)
                else:
                    cur_color = current_tray.get("tray_color", "")
                    cur_type = current_tray.get("tray_type", "")
                    cur_state = current_tray.get("state")
                    fp_color = assignment.fingerprint_color or ""
                    fp_type = assignment.fingerprint_type or ""

                    # SpoolBuddy pre-config replay: fingerprint_type empty means
                    # the slot was empty when the user pre-assigned via SpoolBuddy
                    # (the firmware drops ams_filament_setting on empty slots, so
                    # MQTT was deferred). The moment any filament gets inserted
                    # — Bambu RFID, 3rd-party, or even an existing-but-now-
                    # reconfigured spool — fire the deferred configuration.
                    # The "loaded" signal is state == 11 (Bambu's "filament fed to
                    # extruder" code) OR, on firmwares that don't use the state
                    # enum meaningfully, a non-empty tray_type when state is
                    # NOT one of the firmware's explicit empty signals (9, 10).
                    # state-only was wrong for firmwares that never set 11 — A1
                    # Mini BMCU 01.07.02.00 and P1S Standard AMS 00.00.06.75 both
                    # always report state=3 — so the replay never fired for them
                    # (#1322). The state ∉ {9,10} guard keeps the firmware's
                    # explicit "empty" signals authoritative over any stale
                    # tray_type that might survive the relay's auto-clearing.
                    #
                    # tray_exist_bits comes first because that guard cannot tell
                    # a firmware "empty" from Bambuddy's own: apply_tray_exist_bits
                    # writes state=9 when the bit is 0 and leaves it there when the
                    # bit returns. A non-RFID spool inserted into a pre-assigned
                    # slot brings no tray_type with it, so the stale 9 made this
                    # expression false forever and the deferred config never fired
                    # — the deadlock #1322 removed from the assign path, still in
                    # place here (#3084, #3100).
                    loaded = spool_present(current_tray) is True or (
                        cur_state == 11 or (cur_state not in (9, 10) and cur_type.strip())
                    )
                    if not fp_type.strip() and loaded and assignment.spool:
                        # The fingerprint ends this replay. Not recorded when nothing
                        # could be sent (no MQTT client while the printer reconnects),
                        # so the next AMS change tries again. An attempt that raised is
                        # recorded as before: it may have sent part of the commands and
                        # would raise again on every change, remain% steps included.
                        configured: bool | None = False
                        try:
                            from backend.app.api.routes.inventory import (
                                apply_spool_to_slot_via_mqtt,
                            )

                            configured = await apply_spool_to_slot_via_mqtt(
                                db=db,
                                current_user=None,
                                spool=assignment.spool,
                                printer_id=printer_id,
                                ams_id=assignment.ams_id,
                                tray_id=assignment.tray_id,
                                current_tray_info_idx=current_tray.get("tray_info_idx", ""),
                                current_tray_type=cur_type,
                                current_tray_sub_brands=current_tray.get("tray_sub_brands", ""),
                            )
                        except Exception:
                            logger.exception(
                                "Pre-config apply failed for spool %d on printer %d AMS%d-T%d",
                                assignment.spool_id,
                                printer_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                            configured = None
                        if configured is False:
                            logger.warning(
                                "Pre-config not sent for spool %d on printer %d AMS%d-T%d (no MQTT client); "
                                "retrying on the next AMS change",
                                assignment.spool_id,
                                printer_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                        else:
                            if configured:
                                logger.info(
                                    "SpoolBuddy pre-config applied on insert: spool %d → printer %d AMS%d-T%d",
                                    assignment.spool_id,
                                    printer_id,
                                    assignment.ams_id,
                                    assignment.tray_id,
                                )
                            assignment.fingerprint_color = cur_color
                            assignment.fingerprint_type = cur_type
                        continue

                    if not _colors_similar(cur_color, fp_color) or cur_type.upper() != fp_type.upper():
                        # Blank tray data mid-print is a runout, not a swap: the
                        # firmware clears colour and type when it unloads a spool
                        # it just emptied. Unlinking here would erase the record
                        # of which spool fed the print so far.
                        if printing_now and not cur_color.strip() and not cur_type.strip():
                            logger.info(
                                "Auto-unlink skipped: spool %d AMS%d-T%d — tray data cleared during a running print "
                                "(runout?)",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                            continue
                        # Same reasoning off the print, on firmware's own say-so:
                        # a blank tray report from a slot whose tray_exist_bits
                        # bit is set describes a spool the AMS cannot identify —
                        # a non-RFID one, or one whose slot was reset — not a
                        # spool that was taken out. Deleting the assignment there
                        # threw away the identity the user had supplied, which is
                        # the only place it existed (#3100). A slot the bit calls
                        # empty, or one that carries no bit at all, still unlinks.
                        #
                        # Unless the slot was reported empty first: a removal
                        # already held means a spool came out and another went
                        # in, so the blank report keeps the hold running below
                        # rather than cancelling it (#3186).
                        if (
                            spool_present(current_tray) is True
                            and not cur_color.strip()
                            and not cur_type.strip()
                            and not host.slot_unlink_grace.is_held(
                                printer_id,
                                ("inventory", assignment.ams_id, assignment.tray_id, assignment.spool_id),
                            )
                        ):
                            logger.info(
                                "Auto-unlink skipped: spool %d AMS%d-T%d — slot still occupied, "
                                "tray reports no filament data yet",
                                assignment.spool_id,
                                assignment.ams_id,
                                assignment.tray_id,
                            )
                            continue
                        # A blank report the presence bit does not vouch for is
                        # still only one push. An idle X1C cleared a whole AMS
                        # unit's bits, colour and type for a moment and lost
                        # four saved assignments that way (#3186); unlink only
                        # if the slot is still blank after the grace period.
                        if not cur_color.strip() and not cur_type.strip():
                            hold_key = ("inventory", assignment.ams_id, assignment.tray_id, assignment.spool_id)
                            if not host.slot_unlink_grace.removal_confirmed(printer_id, hold_key):
                                held.add(hold_key)
                                logger.info(
                                    "Auto-unlink held: spool %d AMS%d-T%d — tray reports no filament data; "
                                    "unlinking if it is still blank in %ds",
                                    assignment.spool_id,
                                    assignment.ams_id,
                                    assignment.tray_id,
                                    int(host.slot_unlink_grace.GRACE_SECONDS),
                                )
                                continue
                        # Fingerprint mismatch — but check if tray now matches the
                        # assigned spool (e.g. auto-configure changed the tray).
                        # Both sides are reduced to the type the slot can carry
                        # before comparing: the assign path writes that rather
                        # than the spool's raw material (#2902), so a spool whose
                        # material is a product line — "PLA+", "HTPLA" — reports
                        # back as "PLA" and would otherwise fail this check and
                        # be auto-unlinked from the slot it was just assigned to.
                        # Reducing the printer's side too keeps slots configured
                        # by an older Bambuddy, still reporting "PLA+", matching.
                        spool = assignment.spool
                        if spool:
                            spool_color = (spool.rgba or "FFFFFFFF").upper()
                            # Two ways the assign path can have arrived at the
                            # slot's type, so both count as "we wrote this".
                            # The material column is one; the spool's preset is
                            # the other, and it outranks the material when the
                            # spool has one -- a spool whose material says PLA
                            # and whose preset is "Bambu PLA Aero" puts
                            # PLA-AERO in the slot (#2902). Read from the stored
                            # preset name rather than resolving the preset,
                            # because this runs on every AMS push and a cloud
                            # lookup here would be both slow and unavailable on
                            # the unauthenticated replay path.
                            spool_types = {host.printer_filament_type(spool.material).upper()}
                            if spool.slicer_filament_name:
                                spool_types.add(host.printer_filament_type(spool.slicer_filament_name).upper())
                            # An imported local preset stores its type outright,
                            # which is what the assign path used -- and the name
                            # above may be unset. One keyed read, and only on a
                            # mismatch, which is rare.
                            #
                            # slicer_filament is free text up to fifty characters,
                            # so the digits have to be checked against the range
                            # of the integer primary key they are about to be
                            # compared with. Postgres raises on an out-of-range
                            # integer rather than simply not matching, and that
                            # would poison this session and abandon the rest of
                            # the cleanup pass.
                            lp_ref = (spool.slicer_filament or "").strip()
                            if lp_ref.isdigit() and int(lp_ref) <= 2147483647:
                                from backend.app.models.local_preset import LocalPreset as _LP

                                lp_type = await db.scalar(host.select(_LP.filament_type).where(_LP.id == int(lp_ref)))
                                if lp_type:
                                    spool_types.add(host.printer_filament_type(lp_type).upper())
                            if (
                                _colors_similar(cur_color, spool_color)
                                and host.printer_filament_type(cur_type).upper() in spool_types
                            ):
                                logger.info(
                                    "Auto-unlink: spool %d AMS%d-T%d — fingerprint mismatch but tray matches spool, updating fp",
                                    assignment.spool_id,
                                    assignment.ams_id,
                                    assignment.tray_id,
                                )
                                assignment.fingerprint_color = cur_color
                                assignment.fingerprint_type = cur_type
                                continue
                        logger.info(
                            "Auto-unlink: spool %d AMS%d-T%d — fingerprint mismatch (cur=%s/%s fp=%s/%s spool=%s/%s)",
                            assignment.spool_id,
                            assignment.ams_id,
                            assignment.tray_id,
                            cur_color,
                            cur_type,
                            fp_color,
                            fp_type,
                            spool.rgba if spool else "?",
                            spool.material if spool else "?",
                        )
                        stale.append(assignment)  # Spool changed
            host.slot_unlink_grace.settle(printer_id, "inventory", held)
            # Snapshot slots before delete — ORM attribute access after the
            # commit would refresh against a deleted row.
            unlinked_slots = [(a.ams_id, a.tray_id) for a in stale]
            for a in stale:
                await db.delete(a)
            if stale:
                logger.info("Auto-unlinked %d stale spool assignments for printer %d", len(stale), printer_id)
            # Commit any changes (stale deletions and/or fingerprint updates)
            await db.commit()
            # Tell open browsers the assignment is gone (#2575). Only the manual
            # REST assign/unassign endpoints broadcast this event; without it the
            # frontend's spool-assignments cache keeps rendering the unlinked
            # spool on the slot until an unrelated refetch — which reads exactly
            # like "the fix didn't work" (reporter verified: a browser refresh
            # after the swap showed the correct state all along).
            for ams_id, tray_id in unlinked_slots:
                await host.ws_manager.broadcast(
                    {
                        "type": "spool_assignment_changed",
                        "printer_id": printer_id,
                        "ams_id": ams_id,
                        "tray_id": tray_id,
                    }
                )
    except Exception as e:
        logger.warning("Spool assignment cleanup failed: %s", e, exc_info=True)


async def _expire_spoolman_empty_slots(printer_id: int, ams_data: list, printing_now: bool) -> None:
    """Delete Spoolman slot rows whose held removal has run its grace period.

    The Spoolman half of the #3186 re-check. The full sync in ``on_ams_change``
    talks to Spoolman for every tray; this only needs the local rows, so it
    repeats that pass's empty-slot decision -- a tray with no type or no colour
    (``parse_ams_tray`` returns None for exactly those), not during a print, and
    not in a slot the presence bit calls occupied -- and nothing else.
    """
    from backend.app import main as host

    logger = host.logging.getLogger(__name__)
    try:
        async with host.async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
            from backend.app.services.ams_slot_presence import spool_present

            enabled = await get_setting(db, "spoolman_enabled")
            if not enabled or enabled.lower() != "true":
                return
            sync_mode = await get_setting(db, "spoolman_sync_mode")
            if sync_mode and sync_mode != "auto":
                return

            trays: dict[tuple[int, int], dict] = {}
            for ams_unit in ams_data or []:
                if not isinstance(ams_unit, dict):
                    continue
                for tray in ams_unit.get("tray", []):
                    if isinstance(tray, dict):
                        trays[(int(ams_unit.get("id", 0)), int(tray.get("id", 0)))] = tray

            rows = (
                (
                    await db.execute(
                        host.select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id)
                    )
                )
                .scalars()
                .all()
            )
            held: set[tuple] = set()
            expired: list[tuple[int, int]] = []
            for row in rows:
                tray = trays.get((row.ams_id, row.tray_id))
                if tray is None or printing_now:
                    continue
                if (tray.get("tray_type") or "").strip() and (tray.get("tray_color") or "").strip():
                    continue
                hold_key = ("spoolman", row.ams_id, row.tray_id, row.spoolman_spool_id)
                if spool_present(tray) is True and not host.slot_unlink_grace.is_held(printer_id, hold_key):
                    continue
                if host.slot_unlink_grace.removal_confirmed(printer_id, hold_key):
                    expired.append((row.ams_id, row.tray_id))
                else:
                    held.add(hold_key)
            host.slot_unlink_grace.settle(printer_id, "spoolman", held)
            if not expired:
                return
            # A statement rather than ORM deletes, like the sync pass: the two
            # can overlap, and a row the other already removed must not fail
            # this commit.
            for ams_id, tray_id in expired:
                await db.execute(
                    host.delete(SpoolmanSlotAssignment).where(
                        SpoolmanSlotAssignment.printer_id == printer_id,
                        SpoolmanSlotAssignment.ams_id == ams_id,
                        SpoolmanSlotAssignment.tray_id == tray_id,
                    )
                )
            await db.commit()
            logger.info("Unlinked %d Spoolman slot(s) that stayed empty for printer %d", len(expired), printer_id)
            for ams_id, tray_id in expired:
                await host.ws_manager.broadcast(
                    {
                        "type": "spool_assignment_changed",
                        "printer_id": printer_id,
                        "ams_id": ams_id,
                        "tray_id": tray_id,
                    }
                )
    except Exception as e:
        logger.warning("Spoolman slot re-check failed for printer %s: %s", printer_id, e, exc_info=True)


async def _recheck_held_unlinks(printer_id: int) -> None:
    """Re-run just the slot cleanup against the printer's current AMS state.

    Scheduled by ``slot_unlink_grace`` when it holds a removal. The unlink
    passes otherwise run only when the AMS hash changes, and a slot that went
    empty and stayed empty may never change it again.
    """
    from backend.app import main as host

    status = host.printer_manager.get_status(printer_id)
    # A disconnected printer's state is its last report, not a new one: acting
    # on it would "confirm" a removal nobody has seen for the whole grace
    # period. Leave the holds; the first push after reconnecting decides.
    if status is None or not status.connected:
        return
    ams_raw = status.raw_data.get("ams")
    ams_data = ams_raw.get("ams", []) if isinstance(ams_raw, dict) else ams_raw if isinstance(ams_raw, list) else []
    printing_now = (getattr(status, "state", "") or "").upper() in ("RUNNING", "PAUSE")
    await host._unlink_stale_assignments(printer_id, ams_data, printing_now)
    await host._expire_spoolman_empty_slots(printer_id, ams_data, printing_now)


async def on_ams_change(printer_id: int, ams_data: list):
    """Handle AMS data changes - sync to Spoolman if enabled and auto mode."""
    from backend.app import main as host

    logger = host.logging.getLogger(__name__)

    # Snapshot BEFORE any await: if a print is active, or a finished one is
    # still being charged, skip weight sync later. on_print_complete may pop
    # _active_sessions during our awaits (#880).
    from backend.app.services.usage_tracker import printer_is_charging

    _print_active = printer_is_charging(printer_id)

    # A slot that reports empty while a print is running is a filament runout,
    # not a spool swap: the spool is still physically in the AMS, just
    # consumed. Dropping either inventory backend's slot link there loses the
    # only record of which spool fed the print, so the completion path can't
    # charge the runout segment to anything. Both cleanup passes below consult
    # this; computed once, up front, so neither depends on the other having run.
    _unlink_state = host.printer_manager.get_status(printer_id)
    printing_now = (getattr(_unlink_state, "state", "") or "").upper() in ("RUNNING", "PAUSE")
    # The session alone is not enough: a restart that finds a stale persisted
    # session registers none for the print that is running.
    _print_active = _print_active or printing_now

    # MQTT relay - publish AMS change
    try:
        printer_info = host.printer_manager.get_printer(printer_id)
        if printer_info:
            await host.mqtt_relay.on_ams_change(printer_id, printer_info.name, printer_info.serial_number, ams_data)
    except Exception:
        pass  # Don't fail AMS callback if MQTT fails

    # Broadcast AMS change via WebSocket (bypasses status_key deduplication)
    # This ensures frontend gets immediate updates when AMS slots are configured
    try:
        state = host.printer_manager.get_status(printer_id)
        if state:
            logger.info("[Printer %s] Broadcasting AMS change via WebSocket", printer_id)
            await host.ws_manager.send_printer_status(
                printer_id,
                host.printer_state_to_dict(
                    state,
                    printer_id,
                    host.printer_manager.get_model(printer_id),
                    host.printer_manager.get_drying_targets(printer_id),
                ),
            )
    except Exception as e:
        logger.warning("Failed to broadcast AMS change for printer %s: %s", printer_id, e)

    await host._unlink_stale_assignments(printer_id, ams_data, printing_now)

    # Auto-manage inventory spools from AMS tray data (skip if Spoolman manages AMS).
    # Serialised per-printer via services/ams_assignment_lock: MQTT bursts can deliver
    # two AMS pushes ~30 ms apart, and without the lock both callbacks read
    # "no existing assignment" for the same (printer, ams, tray) and race to
    # INSERT, hitting the spool_assignment_printer_id_ams_id_tray_id_key
    # unique constraint on Postgres. SQLite's WAL serialises writes so the
    # bug stayed latent there. See services/ams_assignment_lock for details.
    try:
        async with host.get_ams_assignment_lock(printer_id), host.async_session() as db:
            from sqlalchemy.orm import selectinload

            from backend.app.api.routes.settings import get_setting
            from backend.app.models.spool import Spool
            from backend.app.models.spool_assignment import SpoolAssignment as SA
            from backend.app.services.spool_tag_matcher import (
                auto_assign_spool,
                create_spool_from_tray,
                find_matching_untagged_spool,
                get_spool_by_tag,
                is_bambu_tag,
                is_valid_tag,
                link_tag_to_inventory_spool,
                tag_belongs_to_archived_spool,
            )

            _spoolman_on = await get_setting(db, "spoolman_enabled")
            _auto_add_raw = await get_setting(db, "auto_add_unknown_rfid")
            _auto_add_unknown = _auto_add_raw is None or _auto_add_raw.lower() == "true"
            if not _spoolman_on or _spoolman_on.lower() != "true":
                for ams_unit in ams_data:
                    if not isinstance(ams_unit, dict):
                        continue
                    ams_id = int(ams_unit.get("id", 0))
                    for tray in ams_unit.get("tray", []):
                        if not isinstance(tray, dict):
                            continue
                        tray_id = int(tray.get("id", 0))
                        tag_uid = tray.get("tag_uid", "")
                        tray_uuid = tray.get("tray_uuid", "")
                        tray_info_idx = tray.get("tray_info_idx", "")
                        if not tray.get("tray_type"):
                            # Slot reported empty — drop any cached unknown-tag
                            # broadcast so reinserting the same spool re-prompts.
                            host._clear_unknown_tag_dedup(printer_id, ams_id, tray_id)
                            continue  # Empty slot
                        # Check if assignment already exists for this slot
                        existing = await db.execute(
                            host.select(SA)
                            .options(selectinload(SA.spool).selectinload(Spool.k_profiles))
                            .where(SA.printer_id == printer_id, SA.ams_id == ams_id, SA.tray_id == tray_id)
                        )
                        existing_assignment = existing.scalar_one_or_none()
                        if existing_assignment:
                            # Sync spool weight_used from AMS remain — only INCREASE, never decrease.
                            # The AMS remain% is low-resolution (integer %, i.e. 10g steps for 1kg spool)
                            # and must not overwrite precise values from the usage tracker (3MF/G-code).
                            # Skip during active prints: the usage tracker handles deduction
                            # precisely via 3MF data on print completion. Without this guard the
                            # AMS remain% SET and the usage tracker ADD both fire from the same
                            # MQTT message, doubling the deduction (#880).
                            if _print_active:
                                continue
                            remain_raw = tray.get("remain")
                            if (
                                remain_raw is not None
                                and existing_assignment.spool
                                and not existing_assignment.spool.weight_locked
                            ):
                                try:
                                    remain_val = int(remain_raw)
                                except (TypeError, ValueError):
                                    remain_val = -1
                                if 1 <= remain_val <= 100:
                                    lw = existing_assignment.spool.label_weight or 1000
                                    new_used = round(lw * (100 - remain_val) / 100.0, 1)
                                    current_used = existing_assignment.spool.weight_used or 0
                                    if new_used > current_used + 1:
                                        # Rechecked in the statement: the spool was loaded
                                        # before this, and a weight set by hand (or weighed)
                                        # and locked since then must stay (WL-6).
                                        result = host.cast(
                                            host.CursorResult,
                                            await db.execute(
                                                host.update(Spool)
                                                .where(
                                                    Spool.id == existing_assignment.spool_id,
                                                    Spool.weight_locked.is_not(True),
                                                    host.func.coalesce(Spool.weight_used, 0) < new_used - 1,
                                                )
                                                .values(weight_used=new_used)
                                            ),
                                        )
                                        await db.commit()
                                        if result.rowcount:
                                            logger.info(
                                                "Weight sync: spool %d weight_used %s -> %s (remain=%d)",
                                                existing_assignment.spool_id,
                                                current_used,
                                                new_used,
                                                remain_val,
                                            )
                                            await host.ws_manager.broadcast({"type": "inventory_changed"})

                            # A K-profile the slot lost (the default K after a reset or
                            # power cycle) is put back by kprofile_drift on the next idle
                            # push; a different real profile was picked on purpose and
                            # stays (#3219, item 44).
                            continue

                        if is_bambu_tag(tag_uid, tray_uuid, tray_info_idx):
                            # BL spool with RFID tag: auto-match → inventory match → auto-create
                            spool = await get_spool_by_tag(db, tag_uid, tray_uuid)
                            if not spool and await tag_belongs_to_archived_spool(db, tag_uid, tray_uuid):
                                # An archived spool still in the AMS: the slot
                                # stays free rather than adopting its tag.
                                continue
                            if not spool:
                                # Try matching an untagged inventory spool (same material/color)
                                spool = await find_matching_untagged_spool(db, tray)
                                if spool:
                                    await link_tag_to_inventory_spool(db, spool, tray)
                                elif _auto_add_unknown:
                                    spool = await create_spool_from_tray(db, tray)
                                else:
                                    # Auto-add disabled: surface the slot so the
                                    # user can add it manually via the UI.
                                    await host._broadcast_unknown_tag(
                                        printer_id=printer_id,
                                        ams_id=ams_id,
                                        tray_id=tray_id,
                                        tag_uid=tag_uid,
                                        tray_uuid=tray_uuid,
                                        tray_type=tray.get("tray_type"),
                                        tray_color=tray.get("tray_color"),
                                        tray_sub_brands=tray.get("tray_sub_brands"),
                                        tray_count=len(ams_unit.get("tray", [])),
                                    )
                                    continue
                            # Slot matched (existing tag, untagged inventory
                            # match, or freshly auto-created spool) — drop any
                            # stale dedup so a future tag swap re-prompts.
                            host._clear_unknown_tag_dedup(printer_id, ams_id, tray_id)
                            await auto_assign_spool(
                                printer_id,
                                ams_id,
                                tray_id,
                                spool,
                                host.printer_manager,
                                db,
                                tray_info_idx=tray_info_idx,
                            )
                            await db.commit()
                            await host.ws_manager.broadcast(
                                {
                                    "type": "spool_auto_assigned",
                                    "printer_id": printer_id,
                                    "ams_id": ams_id,
                                    "tray_id": tray_id,
                                    "spool_id": spool.id,
                                }
                            )
                            logger.info(
                                "RFID auto-assigned spool %d to printer %d AMS%d-T%d",
                                spool.id,
                                printer_id,
                                ams_id,
                                tray_id,
                            )
                        elif is_valid_tag(tag_uid, tray_uuid) and not await tag_belongs_to_archived_spool(
                            db, tag_uid, tray_uuid
                        ):
                            # Non-BL spool with some tag — let user choose (not
                            # an archived one's, whose slot stays free)
                            await host._broadcast_unknown_tag(
                                printer_id=printer_id,
                                ams_id=ams_id,
                                tray_id=tray_id,
                                tag_uid=tag_uid,
                                tray_uuid=tray_uuid,
                                tray_type=tray.get("tray_type"),
                                tray_color=tray.get("tray_color"),
                                tray_sub_brands=tray.get("tray_sub_brands"),
                                tray_count=len(ams_unit.get("tray", [])),
                            )
                        # No-tag slots (generic non-RFID filament) are left alone:
                        # nothing to identify, prompting "+ Add" would just create
                        # ghost spools with empty tags on every confirm.
    except Exception as e:
        logger.warning("RFID spool auto-assign failed: %s", e, exc_info=True)

    try:
        async with host.async_session() as db:
            from backend.app.api.routes.settings import get_setting
            from backend.app.models.printer import Printer

            # Check if Spoolman is enabled
            spoolman_enabled = await get_setting(db, "spoolman_enabled")
            if not spoolman_enabled or spoolman_enabled.lower() != "true":
                return

            # Check sync mode
            sync_mode = await get_setting(db, "spoolman_sync_mode")
            if sync_mode and sync_mode != "auto":
                return  # Only sync on auto mode

            _auto_add_raw_sm = await get_setting(db, "auto_add_unknown_rfid")
            auto_add_unknown_rfid = _auto_add_raw_sm is None or _auto_add_raw_sm.lower() == "true"

            # `spoolman_disable_weight_sync` is deprecated (#1119) — weight is now
            # always owned by per-print tracking, never by AMS auto-sync. The
            # setting is still read by the settings UI for backwards compat but
            # has no effect on the sync path here.

            # Get Spoolman URL
            spoolman_url = await get_setting(db, "spoolman_url")
            if not spoolman_url:
                return

            # Get or create Spoolman client
            client = await host.get_spoolman_client()
            if not client:
                try:
                    client = await host.init_spoolman_client(spoolman_url)
                except ValueError as exc:
                    logger.warning("Spoolman URL %r rejected by SSRF guard: %s", spoolman_url, exc)
                    return

            # Check if Spoolman is reachable
            if not await client.health_check():
                logger.warning("Spoolman not reachable at %s", spoolman_url)
                return

            # Get printer name for location
            result = await db.execute(host.select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            printer_name = printer.name if printer else f"Printer {printer_id}"

            # OPTIMIZATION: Fetch all spools once before processing trays
            # This eliminates redundant API calls (one per tray) when syncing multiple trays
            logger.debug("[Printer %s] Fetching spools cache for AMS sync...", printer_id)
            try:
                cached_spools = await client.get_spools()
                logger.debug("[Printer %s] Cached %d spools for batch sync", printer_id, len(cached_spools))
            except Exception as e:
                logger.error(
                    "[Printer %s] Failed to fetch spools cache after retries, aborting AMS sync: %s",
                    printer_id,
                    e,
                )
                return

            # Load inventory weights as fallback (when AMS MQTT data lacks remain values)
            from sqlalchemy.orm import selectinload

            from backend.app.models.spool_assignment import SpoolAssignment
            from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
            from backend.app.services.ams_slot_presence import spool_present
            from backend.app.services.inventory_mode import spoolman_owns_assignments

            # Built-in remaining weight, used by sync_ams_tray only when the
            # firmware reports an unusable remain%/tray_weight for a slot.
            #
            # Left empty since #2812. This block runs in Spoolman mode only,
            # and until then the built-in table was emptied on the switch, so
            # there was never anything here to read and the fallback was inert.
            # Preserving those rows makes it live again, and it is keyed by slot
            # rather than by spool: after a mode switch the tray may well hold
            # different filament, and ``create_spool`` writes ``remaining_weight``
            # unconditionally, so a stale figure would be seeded into a brand new
            # Spoolman spool. Deliberately kept inert rather than deleted, so the
            # intent survives for whoever revisits the cross-mode fallback.
            inventory_weights: dict[tuple[int, int], float] = {}
            if not await spoolman_owns_assignments(db):
                try:
                    assign_result = await db.execute(
                        host.select(SpoolAssignment)
                        .options(selectinload(SpoolAssignment.spool))
                        .where(SpoolAssignment.printer_id == printer_id)
                    )
                    for assignment in assign_result.scalars().all():
                        spool = assignment.spool
                        if spool and spool.label_weight > 0:
                            remaining = max(0.0, spool.label_weight - (spool.weight_used or 0))
                            inventory_weights[(assignment.ams_id, assignment.tray_id)] = remaining
                except Exception as e:
                    logger.warning("Could not load inventory weights for printer %s: %s", printer_id, e)

            # Load existing Spoolman slot assignments for the no-RFID fallback path
            spoolman_slot_map: dict[tuple[int, int], int] = {}
            slot_map_read = False
            try:
                slot_result = await db.execute(
                    host.select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id)
                )
                for slot in slot_result.scalars().all():
                    spoolman_slot_map[(slot.ams_id, slot.tray_id)] = slot.spoolman_spool_id
                slot_map_read = True
            except Exception as e:
                logger.warning("Could not load Spoolman slot assignments for printer %s: %s", printer_id, e)

            # Sync each AMS tray and collect slot changes for DB persistence
            synced = 0
            slot_changes: list[tuple[int, int, int]] = []  # (ams_id, tray_id, spoolman_spool_id) to upsert
            empty_slots: list[tuple[int, int]] = []  # (ams_id, tray_id) whose tray is now empty
            spoolman_held: set[tuple] = set()  # removals held for the grace period (#3186)
            for ams_unit in ams_data:
                if not isinstance(ams_unit, dict):
                    continue
                ams_id = int(ams_unit.get("id", 0))
                trays = ams_unit.get("tray", [])

                for tray_data in trays:
                    if not isinstance(tray_data, dict):
                        continue
                    tray_id_raw = int(tray_data.get("id", 0))
                    tray = client.parse_ams_tray(ams_id, tray_data)
                    if not tray:
                        # Empty tray slot — record for local assignment cleanup
                        # and drop any cached unknown-tag broadcast so a
                        # reinserted spool re-prompts.
                        #
                        # Not during a running print: a slot that empties there
                        # is a filament runout, and the spool is still in the
                        # AMS. `spoolman_slot_assignments` is how a tag-less
                        # spool assigned through the Bambuddy UI is resolved at
                        # completion (#1459), so deleting the row mid-print
                        # loses the runout segment's usage — the same failure
                        # the internal inventory's auto-unlink had.
                        #
                        # Nor when firmware's presence bit says the slot is
                        # occupied. parse_ams_tray calls a tray with no type or
                        # no colour empty, and a spool the AMS cannot read has
                        # neither until something configures it — so a tag-less
                        # spool assigned through the UI had its row deleted by
                        # the first idle push after it was inserted. Same
                        # deletion as the internal inventory's in #3100, same
                        # answer, so the two modes stay in step.
                        #
                        # And only once the slot has stayed empty for the grace
                        # period -- the internal inventory's #3186 answer, for
                        # the same one-push blank.
                        linked_spool = spoolman_slot_map.get((ams_id, tray_id_raw))
                        hold_key = ("spoolman", ams_id, tray_id_raw, linked_spool)
                        if not printing_now and (
                            spool_present(tray_data) is not True or host.slot_unlink_grace.is_held(printer_id, hold_key)
                        ):
                            if linked_spool is None or host.slot_unlink_grace.removal_confirmed(printer_id, hold_key):
                                empty_slots.append((ams_id, tray_id_raw))
                            else:
                                spoolman_held.add(hold_key)
                                logger.info(
                                    "Spoolman slot unlink held: AMS%d-T%d (spool %d) reports empty; "
                                    "unlinking if it is still empty in %ds",
                                    ams_id,
                                    tray_id_raw,
                                    linked_spool,
                                    int(host.slot_unlink_grace.GRACE_SECONDS),
                                )
                        host._clear_unknown_tag_dedup(printer_id, ams_id, tray_id_raw)
                        continue

                    spool_tag = (
                        tray.tray_uuid
                        if tray.tray_uuid and tray.tray_uuid != "00000000000000000000000000000000"
                        else tray.tag_uid
                    )

                    # Provide the hint only when no RFID is available
                    hint = spoolman_slot_map.get((ams_id, tray.tray_id)) if not spool_tag else None

                    try:
                        inv_remaining = inventory_weights.get((ams_id, tray.tray_id))
                        result = await client.sync_ams_tray(
                            tray,
                            printer_name,
                            db,
                            # Per-print tracking is the only weight writer (#1119).
                            # AMS auto-sync still maintains spool metadata / slot
                            # assignments but no longer touches remaining_weight.
                            disable_weight_sync=True,
                            cached_spools=cached_spools,
                            inventory_remaining=inv_remaining,
                            spoolman_spool_id_hint=hint,
                            auto_add_unknown_rfid=auto_add_unknown_rfid,
                        )
                        if result is None and spool_tag and not auto_add_unknown_rfid:
                            # Spoolman skipped auto-create per user setting — surface
                            # the slot so the UI can offer "+ Add to inventory".
                            await host._broadcast_unknown_tag(
                                printer_id=printer_id,
                                ams_id=ams_id,
                                tray_id=tray.tray_id,
                                tag_uid=tray.tag_uid or "",
                                tray_uuid=tray.tray_uuid or "",
                                tray_type=tray.tray_type,
                                tray_color=tray.tray_color,
                                tray_sub_brands=tray.tray_sub_brands,
                                tray_count=len(trays),
                            )
                        elif result:
                            host._clear_unknown_tag_dedup(printer_id, ams_id, tray.tray_id)
                        if result:
                            synced += 1
                            if result.get("id"):
                                slot_changes.append((ams_id, tray.tray_id, result["id"]))
                                # If a new spool was created, add it to the cache
                                # so subsequent trays can find it if they reference the same tag
                                spool_exists = any(s.get("id") == result["id"] for s in cached_spools)
                                if not spool_exists:
                                    cached_spools.append(result)
                                    logger.debug(
                                        "[Printer %s] Added newly created spool %s to cache",
                                        printer_id,
                                        result["id"],
                                    )
                                # Reconcile slot_preset_mappings (the same row internal
                                # mode keeps in sync via inventory + spool_tag_matcher).
                                # Without this the slot card surfaces the previous spool's
                                # preset name — same bug shape, different inventory mode.
                                from backend.app.services.slot_preset_writer import (
                                    upsert_slot_preset_for_spoolman_spool,
                                )

                                await upsert_slot_preset_for_spoolman_spool(
                                    db=db,
                                    spoolman_spool=result,
                                    tray_info_idx=tray.tray_info_idx or "",
                                    tray_sub_brands=tray.tray_sub_brands or "",
                                    tray_type=tray.tray_type or "",
                                    printer_id=printer_id,
                                    ams_id=ams_id,
                                    tray_id=tray.tray_id,
                                )
                    except Exception as e:
                        logger.error("Error syncing AMS %s tray %s: %s", ams_id, tray.tray_id, e)

            host.slot_unlink_grace.settle(printer_id, "spoolman", spoolman_held)

            if synced > 0:
                logger.info("Auto-synced %s AMS trays to Spoolman for printer %s", synced, printer_id)

            # Persist slot assignment changes to the local table: only slots
            # whose spool this pass changed, and only while they still hold what
            # it read before awaiting Spoolman, so a spool assigned by hand in
            # between stays (SSA-3). The changed slots are announced: the slot
            # card reads slot_preset_mappings (rewritten above) ahead of the live
            # tray, so with no event it kept the previous spool's preset name.
            if slot_changes or empty_slots:
                from backend.app.services.spoolman_slot_sync import write_slot_results

                try:
                    await write_slot_results(
                        db,
                        {(printer_id, a_id, t_id): spool_id for (a_id, t_id), spool_id in spoolman_slot_map.items()}
                        if slot_map_read
                        else None,
                        [(printer_id, a_id, t_id, spool_id) for a_id, t_id, spool_id in slot_changes],
                        [(printer_id, a_id, t_id) for a_id, t_id in empty_slots],
                    )
                except Exception as e:
                    await db.rollback()
                    logger.error("Error persisting Spoolman slot assignments for printer %s: %s", printer_id, e)

    except Exception as e:
        host.logging.getLogger(__name__).error("Spoolman AMS sync failed for printer %s: %s", printer_id, e)


async def _cleanup_sd_card_after_print(
    printer_id: int, subtask_name: str | None, archive_id: int | None, logger
) -> None:
    """Delete a finished print's file from the printer's SD card."""
    # Cleanup: delete uploaded file from printer SD card to prevent phantom prints (Issue #374, #1542)
    # The print scheduler uploads files to the SD card root (/). Some printers (e.g. P1S, A1)
    # auto-start files found in root on power cycle, causing ghost prints.
    # Must run before the archive_id early-return so it executes even when archiving is disabled.
    from backend.app import main as host

    try:
        if subtask_name:
            archive_filename: str | None = None
            async with host.async_session() as db:
                from backend.app.models.archive import PrintArchive
                from backend.app.models.printer import Printer

                result = await db.execute(host.select(Printer).where(Printer.id == printer_id))
                printer = result.scalar_one_or_none()
                if archive_id:
                    archive_row = await db.execute(
                        host.select(PrintArchive.filename).where(PrintArchive.id == archive_id)
                    )
                    archive_filename = archive_row.scalar_one_or_none()

            if printer:
                from backend.app.printers.files import DeleteResult, delete_file_async
                from backend.app.utils.filename import derive_remote_filename

                # Primary candidate: the exact path the dispatcher uploaded to
                # (derived from archive.filename via the same rule as upload).
                # Without it, a library row that ended up with a doubled
                # .gcode.3mf (#1542) leaves the real file behind because the
                # subtask_name + ext fallbacks below don't match what's on the
                # SD card. Fallbacks remain for archive-less prints (subtask
                # never resolved to an archive) and for older naming variants.
                candidate_paths: list[str] = []
                if archive_filename:
                    candidate_paths.append(f"/{derive_remote_filename(archive_filename)}")
                for ext in (".3mf", ".gcode"):
                    fallback = f"/{subtask_name}{ext}"
                    if fallback not in candidate_paths:
                        candidate_paths.append(fallback)

                # Three outcomes track across all candidates so the final log
                # line reflects what actually happened. The A1 in #1721 always
                # ends here with ``any_not_found=True`` and the others False
                # — its firmware auto-cleans the SD card before our cleanup
                # runs, every candidate FTP-DELE returns 550, and the old
                # code burned 3 retries × 2 s × 3 candidates per print
                # logging a misleading "may linger" WARNING on a successful
                # print.
                any_deleted = False
                any_real_failure = False
                any_not_found = False

                files_in_use = host._sd_files_in_use(host.printer_manager.get_status(printer_id))

                for remote_path in candidate_paths:
                    if host.PurePosixPath(remote_path).name.lower() in files_in_use:
                        logger.info(
                            "SD card cleanup: keeping %s on printer %s, which is printing it now",
                            remote_path,
                            printer.name,
                        )
                        continue
                    # Retry only the FAILED case — 550 NOT_FOUND will never
                    # recover by waiting, so a "file isn't here" answer
                    # advances immediately to the next candidate without
                    # consuming the retry budget.
                    for attempt in range(1, 4):
                        try:
                            delete_result = await delete_file_async(
                                printer.ip_address,
                                printer.access_code,
                                remote_path,
                                printer_model=printer.model,
                                printer_id=printer_id,
                            )
                        except Exception as e:
                            delete_result = DeleteResult.FAILED
                            logger.warning(
                                "SD card cleanup attempt %d/3 raised for %s: %s",
                                attempt,
                                remote_path,
                                e,
                            )

                        if delete_result == DeleteResult.DELETED:
                            any_deleted = True
                            logger.info("Deleted %s from printer %s SD card", remote_path, printer.name)
                            break
                        if delete_result == DeleteResult.NOT_FOUND:
                            any_not_found = True
                            break  # 550 will not recover; try next candidate
                        # FAILED: real error — retry with backoff, then give up
                        if attempt < 3:
                            await host.asyncio.sleep(2)
                        else:
                            any_real_failure = True
                            logger.warning(
                                "SD card cleanup failed after 3 attempts for %s "
                                "(network/auth/transient error — file may linger on SD card)",
                                remote_path,
                            )

                if not any_deleted and not any_real_failure and any_not_found:
                    # Every candidate said "not here." Either the printer
                    # firmware swept the SD card itself (common on A1) or the
                    # dispatcher's upload path doesn't match our candidate
                    # rule. Either way: nothing to clean up, no warning.
                    logger.debug(
                        "SD card cleanup: nothing to delete on %s — every candidate returned 550 "
                        "(printer likely self-cleaned)",
                        printer.name,
                    )
    except Exception as e:
        logger.warning("SD card file cleanup failed for printer %s: %s", printer_id, e)

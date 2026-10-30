"""Bambu material inventory projection for legacy host queue workflows.

Numeric AMS addressing and two-nozzle external spool conventions belong to
this package; API2 drivers publish SDK MaterialInventoryItem resource IDs.
"""


def _normalize_color(color: str | None) -> str:
    return f"#{color.replace('#', '')[:6]}" if color else "#808080"


def global_tray_id(ams_id: int, tray_id: int) -> int:
    """Bambu's regular AMS four-slot packing and AMS-HT singleton address."""
    return ams_id if ams_id >= 128 else ams_id * 4 + tray_id


def loaded_materials(status, telemetry: dict) -> list[dict]:
    filaments = []

    # Get ams_extruder_map for dual-nozzle printers (H2D, H2D Pro)
    ams_extruder_map = telemetry.get("ams_extruder_map", {})

    # Routes external spools to an extruder (#2771). Mirrors
    # `buildLoadedFilaments` in the frontend.
    vt_trays = telemetry.get("vt_tray") or []
    nozzles = getattr(status, "nozzles", None) or []
    is_dual_nozzle = bool(
        (len(nozzles) > 1 and getattr(nozzles[1], "nozzle_diameter", ""))
        or ams_extruder_map
        or (isinstance(vt_trays, list) and len(vt_trays) > 1)
    )

    # Parse AMS units from raw_data
    ams_data = telemetry.get("ams", [])
    for ams_unit in ams_data:
        ams_id = int(ams_unit.get("id", 0))
        trays = ams_unit.get("tray", [])
        is_ht = 128 <= ams_id <= 135  # AMS-HT unit ids; a regular AMS may list fewer trays

        for tray in trays:
            tray_type = tray.get("tray_type")
            if tray_type:
                tray_id = int(tray.get("id", 0))
                tray_color = tray.get("tray_color", "")
                # tray_info_idx identifies the specific spool (e.g., "GFA00", "P4d64437")
                tray_info_idx = tray.get("tray_info_idx", "")
                # Normalize color: remove alpha, add hash
                color = _normalize_color(tray_color)
                # Calculate global tray ID
                # AMS-HT units have IDs starting at 128 with a single tray
                tray_address = global_tray_id(ams_id, tray_id)

                filaments.append(
                    {
                        "type": tray_type,
                        "color": color,
                        # `color` reads a missing colour as #808080; this
                        # says whether the printer reported one at all.
                        "color_known": bool(tray_color),
                        "tray_info_idx": tray_info_idx,
                        "ams_id": ams_id,
                        "tray_id": tray_id,
                        "is_ht": is_ht,
                        "is_external": False,
                        "global_tray_id": tray_address,
                        "extruder_id": ams_extruder_map.get(str(ams_id)),
                        "remain": tray.get("remain", -1),
                    }
                )

    # Check external spool(s) (vt_tray is a list)
    for idx, vt in enumerate(vt_trays):
        if vt.get("tray_type"):
            color = _normalize_color(vt.get("tray_color", ""))
            tray_id = int(vt.get("id", 254))
            filaments.append(
                {
                    "type": vt["tray_type"],
                    "color": color,
                    "color_known": bool(vt.get("tray_color")),
                    "tray_info_idx": vt.get("tray_info_idx", ""),
                    "ams_id": -1,
                    "tray_id": idx,
                    "is_ht": False,
                    "is_external": True,
                    "global_tray_id": tray_id,
                    # 254 = VIRTUAL_TRAY_DEPUTY_ID feeds extruder 1 (left),
                    # 255 = VIRTUAL_TRAY_MAIN_ID feeds extruder 0 (right).
                    "extruder_id": (255 - tray_id) if is_dual_nozzle else None,
                    "remain": vt.get("remain", -1),
                }
            )

    return filaments

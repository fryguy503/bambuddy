"""Admission for vendor-specific printer operations.

Anycubic native controls are explicitly admitted alongside LAN observation.
The allowlist prevents new Bambu routes from silently sending FTPS,
calibration or filament commands to a different protocol.
"""

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequestPrinterScope
from backend.app.core.database import get_db
from backend.app.core.printer_scope import PrinterScope
from backend.app.models.printer import Printer

ANYCUBIC_OPERATIONS = frozenset(
    {
        "get_printer",
        "update_printer",
        "discover_anycubic_printer_for_update",
        "delete_printer",
        "get_printer_status",
        "get_overlay_status",
        "get_current_print_user",
        "refresh_printer_status",
        "connect_printer",
        "disconnect_printer",
        "enable_mqtt_logging",
        "disable_mqtt_logging",
        "get_mqtt_logs",
        "clear_mqtt_logs",
        "stop_print",
        "clear_plate",
        "pause_print",
        "resume_print",
        "set_nozzle_temperature",
        "set_bed_temperature",
        "set_fan_speed",
        "set_print_speed",
        "start_drying",
        "stop_drying",
        "set_chamber_light",
        "bed_jog",
        "home_axes",
        "jog_anycubic_axis",
        "pulse_anycubic_extruder",
        "stop_anycubic_extrusion",
        "set_anycubic_slot",
        "feed_anycubic_slot",
        "get_ams_labels",
        "save_ams_label",
        "delete_ams_label",
    }
)


async def require_printer_operation(
    request: Request,
    printer_scope: PrinterScope = RequestPrinterScope,
    db: AsyncSession = Depends(get_db, scope="function"),
) -> None:
    route = request.scope.get("route")
    if getattr(route, "name", None) in ANYCUBIC_OPERATIONS:
        return
    printer_id = request.path_params.get("printer_id")
    if printer_id is None:
        return
    # Scope refusals remain the route's ordinary 404/permission response. Do
    # not reveal a hidden printer's protocol before its permission gate runs.
    try:
        printer_id = int(printer_id)
    except (ValueError, TypeError):
        return
    if not printer_scope.allows(printer_id):
        return
    result = await db.execute(select(Printer.printer_type).where(Printer.id == printer_id))
    if result.scalar_one_or_none() != "anycubic":
        return
    raise HTTPException(
        409,
        detail={
            "code": "printer_operation_unsupported",
            "message": "This operation is not supported by the Anycubic LAN provider.",
        },
    )

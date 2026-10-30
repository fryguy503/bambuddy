import asyncio
import logging
import re
import secrets
import sys
import zipfile
from types import ModuleType

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from backend.app.core import database
from backend.app.core.auth import (
    RequestPrinterScope,
    RequireOverlayTokenIfAuthEnabled,
    RequirePermissionIfAuthEnabled,
    RequirePrinterPermissionIfAuthEnabled,
    is_auth_enabled,
    require_media_token_printer_permission,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import PrinterScope, location_grantees
from backend.app.core.websocket import ws_manager
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.printers.api import execute_host_command
from backend.app.printers.diagnostic import run_connection_diagnostic
from backend.app.printers.files import (
    DownloadDeadlineExceeded,
    FileNotOnPrinterError,
    delete_file_async,
    download_file_bytes_async,
    get_storage_info_async,
    list_files_result_async,
)
from backend.app.schemas.printer import (
    DiagnosticRequest,
    PrinterCreate,
    PrinterDiagnosticResult,
    PrinterFilesDownloadRequest,
    PrinterFilesJobRequest,
    PrinterResponse,
    PrinterResponseWithSecret,
    PrinterStatus,
    PrinterUpdate,
    hms_error_responses as hms_error_responses,
)
from backend.app.services.printer_file_cache import printer_file_cache
from backend.app.services.printer_manager import (
    display_temperatures,
    get_derived_status_name,
    printer_manager,
    printer_state_to_dict,
)
from backend.app.services.printer_media import (
    MAX_PRINTER_ZIP_PREPARE_SECONDS,
    PrinterFileDownloadError,
    PrinterFilesZipInsufficientSpaceError,
    PrinterFilesZipTooLargeError,
    build_printer_file,
    build_printer_files_zip,
    cancel_printer_files_job,
    get_printer_files_job,
    printer_file_path,
    printer_files_zip_path,
    remove_printer_files_zip,
    start_printer_files_job,
)
from backend.app.utils.http import build_content_disposition, download_error_response, safe_download_filename

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/printers", tags=["printers"])

# Seconds the /hms/execute-action route waits for a printer status push
# confirming the command landed before reporting 502 to the UI. Module-level
# so tests can monkeypatch a near-zero value instead of mocking asyncio.sleep.
HMS_ACTION_ACK_WAIT_SECONDS = 2.5



async def _caller_can_view_printer_secrets(user: User | None, db: AsyncSession) -> bool:
    """Whether the caller is trusted enough to see ``access_code`` on a printer
    response. Fail-CLOSED: anything that isn't an authenticated user holding
    PRINTERS_UPDATE returns False.

    - Auth disabled  → True (single trust domain — same as today's local UI).
    - JWT user with PRINTERS_UPDATE → True (Admin or Operator; the same roles
      that already manage printers and the Virtual Printer card UX that
      surfaces a target's code for slicer configuration).
    - JWT Viewer → False (the bug fix: Viewers must not be able to read
      access_code via PRINTERS_READ and then go around Bambuddy to MQTT).
    - API-key principal (``user is None`` because the dep returns None for
      API keys) → False. PRINTERS_UPDATE is admin-only and absent from
      ``_APIKEY_SCOPE_BY_PERMISSION``, so no API key can hold it.
    """
    if not await is_auth_enabled(db):
        return True
    if user is None:
        return False
    return user.has_permission(Permission.PRINTERS_UPDATE.value)


def _serialize_printer(printer: Printer, *, include_secret: bool):
    """Build the response shape that matches the caller's authority."""
    if include_secret:
        return PrinterResponseWithSecret.model_validate(printer)
    return PrinterResponse.model_validate(printer)


@router.get("/")
async def list_printers(
    user: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
    printer_scope: PrinterScope = RequestPrinterScope,
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """List the configured printers the caller may see (#1727).

    ``access_code`` is included in each item only when the caller is trusted
    to see it (Admin / Operator JWT, or auth-disabled mode). Viewers and
    API keys never receive it.
    """
    query = select(Printer).order_by(Printer.name)
    if (clause := printer_scope.where_strict(Printer.id)) is not None:
        query = query.where(clause)
    result = await db.execute(query)
    printers = list(result.scalars().all())
    include_secret = await _caller_can_view_printer_secrets(user, db)
    return [_serialize_printer(p, include_secret=include_secret) for p in printers]


@router.post("/", response_model=PrinterResponse)
async def create_printer(
    printer_data: PrinterCreate,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CREATE),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Add a new printer.

    Verifies the MQTT connection succeeds before persisting. A wrong access
    code or unreachable IP would otherwise create a printer row that shows
    as an empty / never-connecting card on the dashboard — those reports
    were turning into support tickets that all traced back to a mistyped
    access code.
    """
    # Check if serial number already exists
    result = await db.execute(select(Printer).where(Printer.serial_number == printer_data.serial_number))
    if result.scalar_one_or_none():
        raise HTTPException(400, "Printer with this serial number already exists")

    test_result = await printer_manager.test_connection(
        ip_address=printer_data.ip_address,
        serial_number=printer_data.serial_number,
        access_code=printer_data.access_code,
        plugin_id=printer_data.plugin_id,
        model=printer_data.model,
        plugin_options=printer_data.plugin_options,
    )
    if not test_result.get("success"):
        # The frontend renders the user-facing message via i18n on `code`;
        # `message` is an English fallback for non-UI clients (curl / scripts).
        raise HTTPException(
            status_code=400,
            detail={
                "code": "printer_connection_failed",
                "message": (
                    "Could not connect to the printer. Verify IP address, serial number, "
                    "and access code, and confirm LAN-only mode is enabled. "
                    "The printer was not added."
                ),
            },
        )

    printer = Printer(**printer_data.model_dump())
    db.add(printer)
    await db.commit()
    await db.refresh(printer)
    printer_manager.configure_printer(printer)
    # A new printer has no plate references: any under its id are a deleted
    # printer's (SQLite reuses ids; deletes before this left them behind).
    from backend.app.services.plate_detection import remove_printer_references

    remove_printer_references(printer.id)

    # A group given this location reaches the new printer straight away (#1727)
    if await location_grantees(db, [printer.location]):
        await ws_manager.refresh_printer_scopes()

    # Connect to the printer
    if printer.is_active:
        await printer_manager.connect_printer(printer)

    return printer


@router.get("/usb-cameras")
async def list_usb_cameras(
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    """List available USB cameras connected to the system.

    Returns a list of detected V4L2 video devices with their info.
    Only works on Linux systems with V4L2 support.

    Returns:
        List of dicts with {device: str, name: str, capabilities: list, formats?: list}
    """
    from backend.app.services import external_camera

    # v4l2-ctl runs twice per device, with a 5 s timeout each.
    cameras = await asyncio.to_thread(external_camera.list_usb_cameras)
    return {"cameras": cameras}


@router.get("/{printer_id}")
async def get_printer(
    printer_id: int,
    user: User | None = RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Get a specific printer.

    ``access_code`` is included only when the caller is trusted to see it
    (Admin / Operator JWT, or auth-disabled mode). Viewers and API keys
    never receive it.
    """
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")
    include_secret = await _caller_can_view_printer_secrets(user, db)
    return _serialize_printer(printer, include_secret=include_secret)


@router.patch("/{printer_id}", response_model=PrinterResponse)
async def update_printer(
    printer_id: int,
    printer_data: PrinterUpdate,
    user: User | None = RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_UPDATE),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Update a printer."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    update_data = printer_data.model_dump(exclude_unset=True)

    # Groups can be given a location (#1727), so moving a printer between
    # locations changes who can reach it. That is an access change and only an
    # admin may make it; an API key never counts as one.
    access_moved = False
    if "location" in update_data and update_data["location"] != printer.location:
        access_moved = bool(await location_grantees(db, [printer.location, update_data["location"]]))
        if access_moved and await is_auth_enabled(db) and not (user is not None and user.is_admin):
            raise HTTPException(
                403,
                "Moving this printer to another location changes which groups can access it. Only an admin can do that.",
            )

    # Handle nested ROI object - flatten to individual columns
    if "plate_detection_roi" in update_data:
        roi = update_data.pop("plate_detection_roi")
        if roi:
            update_data["plate_detection_roi_x"] = roi.get("x")
            update_data["plate_detection_roi_y"] = roi.get("y")
            update_data["plate_detection_roi_w"] = roi.get("w")
            update_data["plate_detection_roi_h"] = roi.get("h")
        else:
            # Clear ROI if set to null
            update_data["plate_detection_roi_x"] = None
            update_data["plate_detection_roi_y"] = None
            update_data["plate_detection_roi_w"] = None
            update_data["plate_detection_roi_h"] = None

    ip_changed = "ip_address" in update_data and update_data["ip_address"] != printer.ip_address

    for field, value in update_data.items():
        setattr(printer, field, value)

    if any(key in update_data for key in ("ip_address", "access_code", "model", "plugin_options")):
        from backend.app.printers.contracts import PrinterConfig
        from backend.app.printers.registry import plugin_for

        plugin_for(printer).validate_config(PrinterConfig.from_printer(printer))

    await db.commit()
    await db.refresh(printer)

    if access_moved:
        await ws_manager.refresh_printer_scopes()

    # Proxy virtual printers forward to the IP they were started with; the
    # sync restarts the ones aimed at this printer on the new one.
    if ip_changed:
        from backend.app.services.virtual_printer import virtual_printer_manager

        try:
            await virtual_printer_manager.sync_from_db()
        except Exception as e:
            logger.error("Failed to sync virtual printers after printer IP change: %s", e)

    # Reconnect if connection settings changed
    if any(k in update_data for k in ["ip_address", "access_code", "is_active", "model", "plugin_options"]):
        printer_manager.configure_printer(printer)
        printer_manager.disconnect_printer(printer_id)
        if printer.is_active:
            await printer_manager.connect_printer(printer)

    return printer


@router.delete("/{printer_id}")
async def delete_printer(
    printer_id: int,
    delete_archives: bool = True,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_DELETE),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Delete a printer.

    Args:
        printer_id: ID of the printer to delete
        delete_archives: If True (default), delete all print archives for this printer.
                        If False, keep archives but remove their printer association.
    """
    from sqlalchemy import delete as sql_delete

    from backend.app.models.archive import PrintArchive
    from backend.app.models.group import group_printers
    from backend.app.models.maintenance import MaintenanceHistory, PrinterMaintenance
    from backend.app.models.printer_operation import PrinterOperation
    from backend.app.models.scheduled_drying import ScheduledDrying
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    active_operations = await db.scalar(
        select(func.count())
        .select_from(PrinterOperation)
        .where(
            PrinterOperation.printer_id == printer_id,
            PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")),
        )
    )
    if active_operations:
        raise HTTPException(409, "Cannot delete a printer while a printer operation is active")

    if delete_archives:
        # Deleting an archive removes its queue items; one that is printing
        # (a reprint on another printer, say) must not go from under the
        # dispatcher -- the same 409 as DELETE /archives/{id} (#1734).
        from backend.app.models.print_queue import PrintQueueItem

        printing = await db.scalar(
            select(func.count())
            .select_from(PrintQueueItem)
            .where(
                PrintQueueItem.status == "printing",
                PrintQueueItem.archive_id.in_(select(PrintArchive.id).where(PrintArchive.printer_id == printer_id)),
            )
        )
        if printing:
            raise HTTPException(
                409,
                f"Cannot delete this printer's archives — {printing} queue item(s) using them are "
                f"currently printing. Stop the print first, or keep the archives.",
            )

    # What was limited to this printer goes with it, rather than reaching every
    # printer (a provider's NULL printer means all) or, on SQLite, whichever
    # printer is given this id next (an API key's list keeps the id). Providers
    # first: retiring a Bambuddy Notify one commits.
    from backend.app.api.routes.notifications import after_providers_deleted, delete_provider
    from backend.app.core.printer_scope import drop_printers_from_api_keys, drop_printers_from_obico
    from backend.app.models.notification import NotificationProvider

    notify = False
    scoped = await db.execute(select(NotificationProvider).where(NotificationProvider.printer_id == printer_id))
    for provider in scoped.scalars().all():
        notify = await delete_provider(db, provider) or notify

    printer_manager.disconnect_printer(printer_id)

    # Locked from the archive sweep to the commit (PostgreSQL; SQLite's single
    # writer serialises this anyway). A print start adopting or writing an
    # archive for this printer meanwhile then waits and is refused by the
    # printer's foreign key, instead of passing it and being deleted by the
    # ORM delete's cascade below: also an archive the user chose to keep, or
    # another printer's (measured). Not earlier: retiring a Notify provider
    # above commits, which ends a lock; nothing commits from here on.
    await db.execute(select(Printer.id).where(Printer.id == printer_id).with_for_update())

    # An operation could have been admitted while notification cleanup awaited
    # I/O. Recheck under the same printer-row lock used for command admission.
    active_operations = await db.scalar(
        select(func.count())
        .select_from(PrinterOperation)
        .where(
            PrinterOperation.printer_id == printer_id,
            PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")),
        )
    )
    if active_operations:
        raise HTTPException(409, "Cannot delete a printer while a printer operation is active")

    # Operation receipts outlive their commands, but must not survive an ID's
    # reuse on databases that do not enforce foreign-key cascades.
    await db.execute(sql_delete(PrinterOperation).where(PrinterOperation.printer_id == printer_id))

    if delete_archives:
        # Delete all archives for this printer
        await db.execute(sql_delete(PrintArchive).where(PrintArchive.printer_id == printer_id))
    else:
        # Orphan the archives instead of deleting them
        from sqlalchemy import update

        await db.execute(update(PrintArchive).where(PrintArchive.printer_id == printer_id).values(printer_id=None))

    # Delete slot assignments for this printer (SQLite doesn't enforce FK cascades)
    await db.execute(sql_delete(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id))

    # Delete scheduled drying runs for this printer (SQLite doesn't enforce FK cascades)
    await db.execute(sql_delete(ScheduledDrying).where(ScheduledDrying.printer_id == printer_id))

    # Drop it from printer-scoped groups (SQLite doesn't enforce FK cascades)
    await db.execute(sql_delete(group_printers).where(group_printers.c.printer_id == printer_id))

    # Delete maintenance history and items for this printer
    # (SQLite doesn't enforce FK cascades, so do it explicitly)
    maintenance_ids = (
        (await db.execute(select(PrinterMaintenance.id).where(PrinterMaintenance.printer_id == printer_id)))
        .scalars()
        .all()
    )
    if maintenance_ids:
        await db.execute(
            sql_delete(MaintenanceHistory).where(MaintenanceHistory.printer_maintenance_id.in_(maintenance_ids))
        )
        await db.execute(sql_delete(PrinterMaintenance).where(PrinterMaintenance.printer_id == printer_id))

    await drop_printers_from_api_keys(db, [printer_id])
    await drop_printers_from_obico(db, [printer_id])
    # A virtual printer aimed at it goes too: without a target a queue-mode one
    # takes jobs for any printer of its model (deleted below, with its plugs).
    from backend.app.api.routes.virtual_printers import drop_virtual_printer, remove_upload_dir
    from backend.app.models.virtual_printer import VirtualPrinter
    from backend.app.services.virtual_printer import virtual_printer_manager

    aimed = (
        (await db.execute(select(VirtualPrinter.id).where(VirtualPrinter.target_printer_id == printer_id)))
        .scalars()
        .all()
    )
    # Its smart plugs too: without a printer a plug is everyone's, and users and
    # keys limited to other printers could switch what it still powers.
    from backend.app.api.routes.smart_plugs import delete_plug
    from backend.app.models.smart_plug import SmartPlug

    for plug in (await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))).scalars().all():
        await delete_plug(db, plug)
    upload_dirs = []
    for vp_id in aimed:
        await virtual_printer_manager.remove_instance(vp_id)
        upload_dirs.append(await drop_virtual_printer(db, vp_id))

    await db.delete(printer)
    await db.commit()
    await after_providers_deleted(notify=notify)
    for upload_dir in upload_dirs:
        remove_upload_dir(upload_dir)
    if aimed:
        try:
            await virtual_printer_manager.sync_from_db()
        except Exception as e:
            logger.error("Failed to sync virtual printers after printer delete: %s", e)
    # Open connections worked out their printers at connect.
    await ws_manager.refresh_printer_scopes()
    # What is kept for its id in memory and on disk: SQLite gives the id to the
    # next printer created.
    from backend.app.main import forget_printer

    await forget_printer(printer_id)

    return {"status": "deleted", "archives_deleted": delete_archives}


@router.get("/{printer_id}/status", response_model=PrinterStatus)
async def get_printer_status(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Get real-time status of a printer."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")
    return await _printer_status(db, printer)


async def _printer_status(db: AsyncSession, printer: Printer) -> PrinterStatus:
    """The live status of *printer*, as the status endpoints report it."""
    printer_id = printer.id
    state = printer_manager.get_status(printer_id)
    if not state:
        # No MQTT client state — the printer was never connected this run, or it
        # was disconnected manually. The plate-clear gate is Bambuddy-side and
        # persisted, so it still has a truthful value here (#2864); reporting the
        # schema default instead told clients the plate was clean and hid the
        # only control that can release the gate.
        return PrinterStatus(
            id=printer_id,
            name=printer.name,
            connected=False,
            awaiting_plate_clear=printer_manager.is_awaiting_plate_clear(printer_id),
        )

    # Both REST and WebSocket status use the registered addon's serializer.
    # Core adds the archive link because it requires persisted print records.
    snapshot = printer_state_to_dict(
        state,
        printer_id,
        printer.model,
        printer_manager.get_drying_targets(printer_id),
        plugin_id=printer.plugin_id,
    )
    current_archive_id: int | None = None
    if state.state in ("RUNNING", "PAUSE"):
        from backend.app.main import running_print_archive

        archive = await running_print_archive(db, printer_id, state, printing_only=False, by_name=True)
        current_archive_id = archive.id if archive is not None else None
    if state.state not in ("RUNNING", "PAUSE"):
        snapshot["current_plate_id"] = None
    snapshot.update(
        id=printer_id,
        name=printer.name,
        current_archive_id=current_archive_id,
        awaiting_plate_clear=printer_manager.is_awaiting_plate_clear(printer_id),
        ams=snapshot.get("ams") or [],
    )
    return PrinterStatus(**snapshot)


@router.get("/{printer_id}/overlay-status")
async def get_overlay_status(
    printer_id: int,
    _: None = RequireOverlayTokenIfAuthEnabled,
    db: AsyncSession = Depends(get_db, scope="function"),
) -> dict:
    """Everything the streaming overlay (#2613) draws for one printer.

    A token-authenticated sibling of ``get_printer_status`` for embeds with no
    login session — OBS loads ``/overlay/{id}?token=...`` and this feeds it.
    Deliberately flat and minimal (name, model, camera rotation, live print state, and
    the one setting the overlay reads) rather than the full ``PrinterStatus``:
    a token holder gets exactly the fields the overlay renders, nothing more.

    Unlike the Cam Wall feed this *includes the print filename* — the overlay
    names the part on screen — which is why it sits behind its own ``overlay``
    scope rather than ``camwall``.
    """
    from backend.app.api.routes.settings import get_setting

    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    time_format = await get_setting(db, "time_format") or "system"
    state = printer_manager.get_status(printer_id)

    if not state:
        # Never connected this run — mirror get_printer_status()'s disconnected
        # shape so the overlay renders its offline state rather than erroring.
        return {
            "id": printer_id,
            "name": printer.name,
            "model": printer.model,
            "camera_rotation": printer.camera_rotation or 0,
            "connected": False,
            "state": None,
            "current_print": None,
            "gcode_file": None,
            "progress": None,
            "remaining_time": None,
            "layer_num": None,
            "total_layers": None,
            "stg_cur_name": None,
            "temperatures": {},
            "time_format": time_format,
        }

    return {
        "id": printer_id,
        "name": printer.name,
        "model": printer.model,
        "camera_rotation": printer.camera_rotation or 0,
        "connected": state.connected,
        "state": state.state,
        "current_print": state.current_print,
        "gcode_file": state.gcode_file,
        "progress": state.progress,
        "remaining_time": state.remaining_time,
        "layer_num": state.layer_num,
        "total_layers": state.total_layers,
        "stg_cur_name": get_derived_status_name(state, printer.model, plugin_id=printer.plugin_id),
        # Nozzle / bed / chamber readings for the overlay's temperature fields
        # (#1422). Filtered rather than passed through: see display_temperatures.
        "temperatures": display_temperatures(state.temperatures, printer.model, plugin_id=printer.plugin_id),
        "time_format": time_format,
    }


@router.get("/{printer_id}/current-print-user")
async def get_current_print_user(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Get the user who started the current print (for reprint tracking).

    Returns user info if available, empty object otherwise.
    This tracks users for reprints (which bypass the queue).
    For queue-based prints, use the queue item's created_by field instead.
    """
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    user_info = printer_manager.get_current_print_user(printer_id)
    if not user_info:
        return {}
    return {"user_id": user_info["user_id"], "username": user_info["username"]}


@router.post("/{printer_id}/refresh-status")
async def refresh_printer_status(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Request a full status refresh from the printer (sends pushall command)."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    success = printer_manager.request_status_update(printer_id)
    if not success:
        raise HTTPException(400, "Printer not connected")

    return {"status": "refresh_requested"}


@router.post("/{printer_id}/connect")
async def connect_printer(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Manually connect to a printer."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    success = await printer_manager.connect_printer(printer)
    return {"connected": success}


@router.post("/{printer_id}/disconnect")
async def disconnect_printer(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Manually disconnect from a printer."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    printer_manager.disconnect_printer(printer_id)
    return {"connected": False}


@router.post("/diagnostic", response_model=PrinterDiagnosticResult)
async def diagnose_connection(
    req: DiagnosticRequest,
    _=RequirePermissionIfAuthEnabled(Permission.PRINTERS_CREATE),
):
    """Run connection diagnostics for the Add-Printer flow (printer not yet saved).

    When serial_number + access_code are supplied the MQTT credential check
    also runs; otherwise only the network-level checks are performed.
    """
    return await run_connection_diagnostic(
        req.ip_address,
        serial_number=req.serial_number or None,
        access_code=req.access_code or None,
        plugin_id=req.plugin_id,
    )


@router.get("/{printer_id}/diagnostic", response_model=PrinterDiagnosticResult)
async def diagnose_printer(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    """Run connection diagnostics for an existing saved printer.

    On-demand run from the UI: wait up to PUBLISH_WAIT_DEFAULT seconds for the
    printer to publish a status report so a fresh reconnect (counter reset to
    0) isn't reported as `printer_publishing: fail` prematurely. The support
    package code path calls run_connection_diagnostic without the wait so
    bundling stays fast.
    """
    from backend.app.services.printer_diagnostic import PUBLISH_WAIT_DEFAULT

    # A short session, released before the probes and the wait (as RC-C9).
    async with database.async_session() as db:
        printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")
    return await run_connection_diagnostic(
        printer.ip_address,
        printer=printer,
        plugin_id=printer.plugin_id,
        wait_for_publish_seconds=PUBLISH_WAIT_DEFAULT,
    )


async def _load_printer_or_404(printer_id: int) -> Printer:
    """Load a printer in a short-lived session, releasing the pooled DB
    connection before the caller starts any FTP/network I/O (#2572).

    The file-manager and storage routes talk FTP to the printer, which can
    block for the full socket timeout — longer when a saturated FTP pool backs
    up. Holding the request's Depends(get_db, scope="function") session across that FTP pinned one
    pooled connection idle-in-transaction per in-flight request, a top cause of
    pool exhaustion on large farms. The returned row's scalar columns stay
    readable after the session closes (expire_on_commit=False). Raises 404 when
    the printer doesn't exist.

    Reference async_session via the module so the maker is resolved at call time
    — keeps it in sync with reinitialize_database() and lets tests patch it.
    """
    async with database.async_session() as db:
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")
    return printer


@router.get("/{printer_id}/files")
async def list_printer_files(
    printer_id: int,
    path: str = "/",
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    """List files on the printer at the specified path."""
    printer = await _load_printer_or_404(printer_id)

    listing = await list_files_result_async(
        printer.ip_address,
        printer.access_code,
        path,
        printer_model=printer.model,
        printer_id=printer_id,
    )
    files = listing.files

    # Add full path to each file
    for f in files:
        f["path"] = f"{path.rstrip('/')}/{f['name']}" if path != "/" else f"/{f['name']}"

    return {
        "path": path,
        "files": files,
        # Slow is not gone: a large folder on a slow card can outlast the wait (TO-C13).
        "warnings": (
            [] if listing.available else ["listing_timed_out"] if listing.timed_out else ["printer_unavailable"]
        ),
    }


@router.get("/{printer_id}/files/download")
async def download_printer_file(
    printer_id: int,
    path: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    """Download a file from the printer."""
    printer = await _load_printer_or_404(printer_id)

    try:
        async with asyncio.timeout(MAX_PRINTER_ZIP_PREPARE_SECONDS):
            result = await build_printer_file(
                printer,
                path,
                None,
                bundle_key=f"single-{secrets.token_urlsafe(18)}",
            )
    except PrinterFilesZipTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    except PrinterFilesZipInsufficientSpaceError as exc:
        raise HTTPException(507, str(exc)) from exc
    except PrinterFileDownloadError as exc:
        raise HTTPException(502, f"Could not download {path} from the printer") from exc
    except FileNotFoundError:
        raise HTTPException(404, f"File not found: {path}")
    except TimeoutError as exc:
        raise HTTPException(504, str(exc) or "Printer download exceeded the 30-minute limit") from exc

    # Determine content type based on extension
    filename = path.split("/")[-1]
    ext = filename.lower().split(".")[-1] if "." in filename else ""

    content_types = {
        "3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        "gcode": "text/plain",
        "mp4": "video/mp4",
        "avi": "video/x-msvideo",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "json": "application/json",
        "txt": "text/plain",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    return FileResponse(
        path=result.path,
        filename=filename,
        media_type=content_type,
        headers={"Content-Disposition": build_content_disposition(filename)},
        background=BackgroundTask(remove_printer_files_zip, result.path),
    )


async def _download_printer_file_bytes(printer, path: str) -> bytes:
    """Download a printer file for a preview: 404 missing, 502 failed, 504 too slow.

    A failed download used to be reported as 404 "File not found" for a file the
    browser had just listed. One FTP session, no listing first, and shared: the
    plate picker asks once per plate, all at once, and each request downloaded
    the whole 3MF again (see services/printer_file_cache).
    """

    async def fetch() -> bytes:
        try:
            data = await download_file_bytes_async(
                printer.ip_address,
                printer.access_code,
                path,
                printer_model=printer.model,
                report_missing=True,
                # The size the printer reports buys a slow link its time, up to
                # the file browser's budget: print start's 300 s ceiling would
                # add none.
                max_total_timeout=MAX_PRINTER_ZIP_PREPARE_SECONDS,
                printer_id=getattr(printer, "id", None),
            )
        except FileNotOnPrinterError as exc:
            raise HTTPException(404, f"File not found: {path}") from exc
        except DownloadDeadlineExceeded as exc:
            raise HTTPException(504, f"Downloading {path} from the printer took longer than its size allows") from exc
        if data is None:
            raise HTTPException(502, f"Could not download {path} from the printer")
        return data

    return await printer_file_cache.get(printer.id, path, fetch)


@router.get("/{printer_id}/files/gcode")
async def get_printer_file_gcode(
    printer_id: int,
    path: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    """Get gcode for a file stored on a printer (for preview)."""
    import io

    printer = await _load_printer_or_404(printer_id)

    data = await _download_printer_file_bytes(printer, path)

    filename = path.split("/")[-1]
    lower = filename.lower()

    if lower.endswith(".gcode"):
        return Response(content=data, media_type="text/plain")
    if lower.endswith(".3mf"):
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                gcode_files = [n for n in zf.namelist() if n.endswith(".gcode")]
                if not gcode_files:
                    raise HTTPException(status_code=404, detail="No gcode found in 3MF file")
                gcode_content = zf.read(gcode_files[0])
                return Response(content=gcode_content, media_type="text/plain")
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid 3MF file")

    raise HTTPException(status_code=400, detail="Unsupported file type")


@router.get("/{printer_id}/files/plates")
async def get_printer_file_plates(
    printer_id: int,
    path: str = Query(..., description="Full path to the 3MF file on the printer"),
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    """Get available plates from a multi-plate 3MF file stored on a printer."""
    import io
    import json

    import defusedxml.ElementTree as ET

    printer = await _load_printer_or_404(printer_id)

    filename = path.split("/")[-1]
    if not filename.lower().endswith(".3mf"):
        return {
            "printer_id": printer_id,
            "path": path,
            "filename": filename,
            "plates": [],
            "is_multi_plate": False,
        }

    data = await _download_printer_file_bytes(printer, path)

    plates = []

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            namelist = zf.namelist()

            # Find all plate gcode files to determine available plates
            gcode_files = [n for n in namelist if n.startswith("Metadata/plate_") and n.endswith(".gcode")]

            # If no gcode is present (source-only or unsliced), fall back to plate JSON/PNG
            plate_indices: list[int] = []
            if gcode_files:
                for gf in gcode_files:
                    try:
                        plate_str = gf[15:-6]  # Remove "Metadata/plate_" and ".gcode"
                        plate_indices.append(int(plate_str))
                    except ValueError:
                        pass  # Skip gcode files with non-numeric plate indices
            else:
                plate_json_files = [n for n in namelist if n.startswith("Metadata/plate_") and n.endswith(".json")]
                plate_png_files = [
                    n
                    for n in namelist
                    if n.startswith("Metadata/plate_")
                    and n.endswith(".png")
                    and "_small" not in n
                    and "no_light" not in n
                ]
                plate_name_candidates = plate_json_files + plate_png_files
                plate_re = re.compile(r"^Metadata/plate_(\d+)\.(json|png)$")
                seen_indices: set[int] = set()
                for name in plate_name_candidates:
                    match = plate_re.match(name)
                    if match:
                        try:
                            index = int(match.group(1))
                        except ValueError:
                            continue
                        if index in seen_indices:
                            continue
                        seen_indices.add(index)
                        plate_indices.append(index)

            if not plate_indices:
                return {
                    "printer_id": printer_id,
                    "path": path,
                    "filename": filename,
                    "plates": [],
                    "is_multi_plate": False,
                }

            plate_indices.sort()

            # Parse model_settings.config for plate names
            plate_names = {}
            if "Metadata/model_settings.config" in namelist:
                try:
                    model_content = zf.read("Metadata/model_settings.config").decode()
                    model_root = ET.fromstring(model_content)
                    for plate_elem in model_root.findall(".//plate"):
                        plater_id = None
                        plater_name = None
                        for meta in plate_elem.findall("metadata"):
                            key = meta.get("key")
                            value = meta.get("value")
                            if key == "plater_id" and value:
                                try:
                                    plater_id = int(value)
                                except ValueError:
                                    pass  # Skip plate with unparseable ID
                            elif key == "plater_name" and value:
                                plater_name = value.strip()
                        if plater_id is not None and plater_name:
                            plate_names[plater_id] = plater_name
                except Exception:
                    pass  # Plate names are optional; continue without them

            # Parse slice_info.config for plate metadata
            plate_metadata = {}
            if "Metadata/slice_info.config" in namelist:
                content = zf.read("Metadata/slice_info.config").decode()
                root = ET.fromstring(content)

                for plate_elem in root.findall(".//plate"):
                    plate_info = {"filaments": [], "prediction": None, "weight": None, "name": None, "objects": []}

                    plate_index = None
                    for meta in plate_elem.findall("metadata"):
                        key = meta.get("key")
                        value = meta.get("value")
                        if key == "index" and value:
                            try:
                                plate_index = int(value)
                            except ValueError:
                                pass  # Skip plate with unparseable index
                        elif key == "prediction" and value:
                            try:
                                plate_info["prediction"] = int(value)
                            except ValueError:
                                pass  # Skip unparseable prediction; leave as None
                        elif key == "weight" and value:
                            try:
                                plate_info["weight"] = float(value)
                            except ValueError:
                                pass  # Skip unparseable weight; leave as None

                    # Get filaments used in this plate
                    for filament_elem in plate_elem.findall("filament"):
                        filament_id = filament_elem.get("id")
                        filament_type = filament_elem.get("type", "")
                        filament_color = filament_elem.get("color", "")
                        used_g = filament_elem.get("used_g", "0")
                        used_m = filament_elem.get("used_m", "0")

                        try:
                            used_grams = float(used_g)
                        except (ValueError, TypeError):
                            used_grams = 0

                        if used_grams > 0 and filament_id:
                            plate_info["filaments"].append(
                                {
                                    "slot_id": int(filament_id),
                                    "type": filament_type,
                                    "color": filament_color,
                                    # The preset the slicer assigned (GFA01, ...): names the product line
                                    "tray_info_idx": filament_elem.get("tray_info_idx", "") or "",
                                    "used_grams": round(used_grams, 1),
                                    "used_meters": float(used_m) if used_m else 0,
                                }
                            )

                    plate_info["filaments"].sort(key=lambda x: x["slot_id"])

                    # Collect object names
                    for obj_elem in plate_elem.findall("object"):
                        obj_name = obj_elem.get("name")
                        if obj_name and obj_name not in plate_info["objects"]:
                            plate_info["objects"].append(obj_name)

                    # Set plate name
                    if plate_index is not None:
                        custom_name = plate_names.get(plate_index)
                        if custom_name:
                            plate_info["name"] = custom_name
                        elif plate_info["objects"]:
                            plate_info["name"] = plate_info["objects"][0]
                        plate_metadata[plate_index] = plate_info

            # Parse plate_*.json for object lists when slice_info is missing
            plate_json_objects: dict[int, list[str]] = {}
            for name in namelist:
                match = re.match(r"^Metadata/plate_(\d+)\.json$", name)
                if not match:
                    continue
                try:
                    plate_index = int(match.group(1))
                except ValueError:
                    continue
                try:
                    payload = json.loads(zf.read(name).decode())
                    bbox_objects = payload.get("bbox_objects", [])
                    names: list[str] = []
                    for obj in bbox_objects:
                        obj_name = obj.get("name") if isinstance(obj, dict) else None
                        if obj_name and obj_name not in names:
                            names.append(obj_name)
                    if names:
                        plate_json_objects[plate_index] = names
                except Exception:
                    continue

            # Build plate list
            for idx in plate_indices:
                meta = plate_metadata.get(idx, {})
                has_thumbnail = f"Metadata/plate_{idx}.png" in namelist
                objects = meta.get("objects", [])
                if not objects:
                    objects = plate_json_objects.get(idx, [])

                plate_name = meta.get("name")
                if not plate_name:
                    plate_name = plate_names.get(idx)
                if not plate_name and objects:
                    plate_name = objects[0]

                plates.append(
                    {
                        "index": idx,
                        "name": plate_name,
                        "objects": objects,
                        "object_count": len(objects),
                        "has_thumbnail": has_thumbnail,
                        "thumbnail_url": f"/api/v1/printers/{printer_id}/files/plate-thumbnail/{idx}?path={path}",
                        "print_time_seconds": meta.get("prediction"),
                        "filament_used_grams": meta.get("weight"),
                        "filaments": meta.get("filaments", []),
                    }
                )

    except Exception as e:
        logger.warning("Failed to parse plates from printer file %s: %s", path, e)

    return {
        "printer_id": printer_id,
        "path": path,
        "filename": filename,
        "plates": plates,
        "is_multi_plate": len(plates) > 1,
    }


@router.get("/{printer_id}/files/plate-thumbnail/{plate_index}")
async def get_printer_file_plate_thumbnail(
    printer_id: int,
    plate_index: int,
    path: str = Query(..., description="Full path to the 3MF file on the printer"),
    _=Depends(require_media_token_printer_permission(Permission.PRINTERS_FILES)),
):
    """Get a plate thumbnail image from a printer-stored 3MF file."""
    import io

    printer = await _load_printer_or_404(printer_id)

    data = await _download_printer_file_bytes(printer, path)

    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            thumb_path = f"Metadata/plate_{plate_index}.png"
            if thumb_path in zf.namelist():
                image_data = zf.read(thumb_path)
                return Response(content=image_data, media_type="image/png")
    except Exception:
        pass  # Corrupt or unreadable 3MF; fall through to 404

    raise HTTPException(status_code=404, detail=f"Thumbnail for plate {plate_index} not found")


@router.post("/{printer_id}/files/download-zip")
async def download_printer_files_as_zip(
    printer_id: int,
    request: PrinterFilesDownloadRequest,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    """Download multiple files using a disk-backed ZIP.

    Kept backward-compatible for API clients: relative paths are rooted,
    duplicate paths receive collision-safe names, and an all-failed request
    returns an empty ZIP as the historical endpoint did. The browser uses the
    asynchronous preparation endpoints below.
    """
    if not request.paths:
        raise HTTPException(400, "No files specified")
    printer = await _load_printer_or_404(printer_id)
    normalized_paths = [path if path.startswith("/") else f"/{path}" for path in request.paths]
    normalized_sizes = {path if path.startswith("/") else f"/{path}": size for path, size in request.sizes.items()}
    try:
        async with asyncio.timeout(MAX_PRINTER_ZIP_PREPARE_SECONDS):
            result = await build_printer_files_zip(
                printer,
                normalized_paths,
                normalized_sizes,
                preserve_paths=False,
                allow_empty=True,
            )
    except PrinterFilesZipTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    except PrinterFilesZipInsufficientSpaceError as exc:
        raise HTTPException(507, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "Printer ZIP preparation exceeded the 30-minute limit") from exc
    return FileResponse(
        path=result.path,
        filename="printer-files.zip",
        media_type="application/zip",
        headers={
            "X-Bambuddy-Files-Requested": str(result.requested),
            "X-Bambuddy-Files-Downloaded": str(result.successful),
            "X-Bambuddy-Files-Failed": str(len(result.failed_paths)),
        },
        background=BackgroundTask(remove_printer_files_zip, result.path),
    )


@router.post("/{printer_id}/files/download-job")
async def create_printer_files_download_job(
    printer_id: int,
    request: PrinterFilesJobRequest,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    """Start a cancellable disk-backed preparation without holding the request."""

    if not request.paths:
        raise HTTPException(400, "No files specified")
    if len(set(request.paths)) != len(request.paths):
        raise HTTPException(400, "Selected printer paths must be unique")
    if not request.as_zip and len(request.paths) != 1:
        raise HTTPException(400, "Native downloads require exactly one file")
    printer = await _load_printer_or_404(printer_id)
    try:
        status = await start_printer_files_job(
            printer,
            request.paths,
            request.sizes,
            request.filename,
            as_zip=request.as_zip,
        )
    except PrinterFilesZipTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    except PrinterFilesZipInsufficientSpaceError as exc:
        raise HTTPException(507, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return status.__dict__


@router.get("/{printer_id}/files/download-jobs/{job_id}")
async def get_printer_files_download_job(
    printer_id: int,
    job_id: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    status = await get_printer_files_job(job_id, printer_id)
    if status is None:
        raise HTTPException(404, "Printer download job not found")
    return status.__dict__


@router.delete("/{printer_id}/files/download-jobs/{job_id}")
async def cancel_printer_files_download_job(
    printer_id: int,
    job_id: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    if not await cancel_printer_files_job(job_id, printer_id):
        raise HTTPException(404, "Printer download job not found")
    return {"status": "cancelled"}


@router.get("/{printer_id}/files/dl/{token}/{filename}")
async def download_prepared_printer_files(
    printer_id: int,
    token: str,
    filename: str,
):
    """Consume a resource-bound token and stream a prepared file natively."""

    from backend.app.core.auth import verify_slicer_download_token

    if not await verify_slicer_download_token(token, "printer-files", printer_id):
        return download_error_response(403, "This download link has already been used or has expired.")
    zip_path = printer_files_zip_path(printer_id, token)
    raw_path = printer_file_path(printer_id, token)
    if zip_path is not None and await asyncio.to_thread(zip_path.is_file):
        prepared_path = zip_path
        media_type = "application/zip"
    elif raw_path is not None and await asyncio.to_thread(raw_path.is_file):
        prepared_path = raw_path
        media_type = "application/octet-stream"
    else:
        return download_error_response(404, "The prepared download is no longer on the server.")
    safe_filename = safe_download_filename(filename, fallback="printer-download")
    return FileResponse(
        path=prepared_path,
        filename=safe_filename,
        media_type=media_type,
        headers={"Content-Disposition": build_content_disposition(safe_filename)},
        background=BackgroundTask(remove_printer_files_zip, prepared_path),
    )


@router.delete("/{printer_id}/files")
async def delete_printer_file(
    printer_id: int,
    path: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    """Delete a file from the printer."""
    printer = await _load_printer_or_404(printer_id)

    from backend.app.printers.files import DeleteResult

    result = await delete_file_async(
        printer.ip_address, printer.access_code, path, printer_model=printer.model, printer_id=printer_id
    )
    if result != DeleteResult.FAILED:
        # Gone from the printer: a preview must not be served from the old copy.
        printer_file_cache.forget(printer_id, path)
    if result == DeleteResult.NOT_FOUND:
        raise HTTPException(404, f"File not found on printer: {path}")
    if result == DeleteResult.FAILED:
        raise HTTPException(500, f"Failed to delete file: {path}")

    return {"status": "deleted", "path": path}


@router.get("/{printer_id}/storage")
async def get_printer_storage(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
):
    """Get storage information from the printer."""
    printer = await _load_printer_or_404(printer_id)

    storage_info = await get_storage_info_async(
        printer.ip_address, printer.access_code, printer_model=printer.model, printer_id=printer_id
    )

    return storage_info or {"used_bytes": None, "free_bytes": None}


@router.post("/{printer_id}/print/stop")
async def stop_print(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Stop/cancel the current print job."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client:
        raise HTTPException(400, "Printer not connected")

    # Sends the stop and marks the printer as stopped by the user, so the
    # resulting "failed"/"aborted" status ends the print as cancelled.
    from backend.app.services.print_control import stop_print_by_user_async

    if not await stop_print_by_user_async(printer_id):
        raise HTTPException(500, "Failed to stop print")

    return {"success": True, "message": "Print stop command sent"}


@router.post("/{printer_id}/clear-plate")
async def clear_plate(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CLEAR_PLATE),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Acknowledge that the build plate has been cleared after a finished/failed print.

    Sets a plate-cleared flag so the scheduler can start the next queued print.
    No MQTT command is sent to the printer — the scheduler's start_print command
    will override the FINISH/FAILED state when it sends the next job.
    """
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    # Deliberately NOT gated on the printer being connected. Acknowledging the plate
    # only mutates Bambuddy-side state — no MQTT command is sent — and with Auto Power
    # Off the normal end-of-print state is exactly this: gate up, printer powered down.
    # The guard this replaces was inherited from the sibling stop/pause/resume handlers,
    # where reaching the printer IS required, and left farms with no way to release the
    # gate short of powering each printer back on by hand (#2864).

    # Accept the acknowledgment whenever the printer is awaiting it — not only when the
    # reported state is FINISH/FAILED. After a power cycle the printer boots into IDLE
    # but the awaiting flag persists, and the user still needs a way to ack it (#961).
    state = printer_manager.get_status(printer_id)
    awaiting = printer_manager.is_awaiting_plate_clear(printer_id)
    if not awaiting and (not state or state.state not in ("FINISH", "FAILED")):
        raise HTTPException(
            400,
            f"Printer is not awaiting plate-clear acknowledgment (state={state.state if state else 'unknown'})",
        )

    printer_manager.set_awaiting_plate_clear(printer_id, False)

    # #1898: releasing the plate without answering the outcome prompt can
    # count as "good" (opt-in setting) — this is the moment the operator
    # moves on, so an unanswered prompt would otherwise linger unconfirmed.
    from backend.app.api.routes.settings import get_setting, setting_is_true

    # setting_is_true rather than a comparison of our own: one reader deciding
    # for itself what "on" spells is how two parts of the app end up
    # disagreeing about the same row.
    if setting_is_true(await get_setting(db, "confirm_default_good_on_plate_clear")):
        from backend.app.services.print_confirmation import resolve_pending_confirmation_as_good

        resolved = await resolve_pending_confirmation_as_good(db, printer_id)
        if resolved is not None:
            await db.commit()

    return {"success": True, "message": "Plate cleared, next print will start shortly"}


@router.post("/{printer_id}/print/pause")
async def pause_print(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Pause the current print job."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client:
        raise HTTPException(400, "Printer not connected")

    success = await execute_host_command(client, "pause_print")
    if not success:
        raise HTTPException(500, "Failed to pause print")

    return {"success": True, "message": "Print pause command sent"}


@router.post("/{printer_id}/print/resume")
async def resume_print(
    printer_id: int,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Resume a paused print job."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(404, "Printer not found")

    client = printer_manager.get_client(printer_id)
    if not client:
        raise HTTPException(400, "Printer not connected")

    success = await execute_host_command(client, "resume_print")
    if not success:
        raise HTTPException(500, "Failed to resume print")

    return {"success": True, "message": "Print resume command sent"}




def __getattr__(name):
    """Deprecated private endpoints remain owned by the active vendor package."""
    if name.startswith("__"):
        raise AttributeError(name)
    from backend.app.printers.compat import compatibility_module

    module = compatibility_module("bambu", "legacy_routes")
    try:
        return getattr(module, name)
    except AttributeError:
        raise AttributeError(name) from None


# Preserve overrides made through the deprecated module path. Python function
# globals stay in their defining plugin module after extraction, so forwarding
# reads alone would make an override on this facade ineffective there.
class _PrinterRouteCompatibilityModule(ModuleType):
    _forwarded_names: set[str] = set()

    def __setattr__(self, name, value):
        if not name.startswith("__"):
            from backend.app.printers.compat import compatibility_module

            implementation = compatibility_module("bambu", "legacy_routes")
            if name in implementation.__dict__ or name in self._forwarded_names:
                self._forwarded_names.add(name)
                setattr(implementation, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if not name.startswith("__"):
            from backend.app.printers.compat import compatibility_module

            implementation = compatibility_module("bambu", "legacy_routes")
            if name in implementation.__dict__:
                delattr(implementation, name)
        if name in self.__dict__:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _PrinterRouteCompatibilityModule

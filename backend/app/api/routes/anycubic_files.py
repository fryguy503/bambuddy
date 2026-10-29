"""Native Anycubic LAN catalogs and upload-only jobs, with printer scope."""

import asyncio
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from sqlalchemy import select

from backend.app.core import database
from backend.app.core.auth import RequirePrinterPermissionIfAuthEnabled
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.services.anycubic import AnycubicMQTTClient
from backend.app.services.anycubic.files import MAX_GCODE_BYTES, validate_gcode
from backend.app.services.anycubic.protocol import AnycubicError
from backend.app.services.anycubic.transfers import transfers
from backend.app.services.printer_manager import printer_manager

router = APIRouter(prefix="/printers/{printer_id}/anycubic/files", tags=["Anycubic files"])
_receiving = set()


async def _printer(printer_id):
    async with database.async_session() as db:
        printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if printer is None:
        raise HTTPException(404, "Printer not found")
    if printer.printer_type != "anycubic":
        raise HTTPException(409, "This file transport requires an Anycubic LAN printer")
    return printer


def _connection(printer):
    client = printer_manager.get_client(printer.id)
    if (
        not isinstance(client, AnycubicMQTTClient)
        or not client.state.connected
        or client.is_stale
        or client.ip_address != printer.ip_address
        or client.serial_number != printer.serial_number
        or client.model != printer.model
        or client.tls_fingerprint != printer.tls_fingerprint
    ):
        raise HTTPException(503, "Connect the saved Anycubic printer before browsing or uploading files")
    return client


@router.get("")
async def list_files(
    printer_id: int,
    path: str = "/",
    page: int = Query(1, ge=1, le=10000),
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    client = _connection(await _printer(printer_id))
    try:
        return await asyncio.to_thread(client.list_files, path=path, page=page)
    except AnycubicError as exc:
        raise HTTPException(503, str(exc)) from None


@router.post("/upload", status_code=202)
async def upload_file(
    printer_id: int,
    file: UploadFile = File(...),
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
    __=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    printer = await _printer(printer_id)
    client = _connection(printer)
    if printer_id in _receiving or transfers.active(printer_id):
        raise HTTPException(409, "A file transfer to this printer is already active")
    _receiving.add(printer_id)
    try:
        staging = Path(tempfile.mkdtemp(prefix="bambuddy-anycubic-upload-"))
    except OSError:
        _receiving.discard(printer_id)
        raise HTTPException(503, "Temporary file storage is unavailable") from None
    path = staging / "export.gcode"
    handed_off = False
    try:
        size = 0
        with path.open("wb") as stream:
            while chunk := await file.read(65536):
                size += len(chunk)
                if size > MAX_GCODE_BYTES:
                    raise HTTPException(413, "G-code uploads must not exceed 512 MiB")
                await asyncio.to_thread(stream.write, chunk)
        try:
            artifact = await asyncio.to_thread(validate_gcode, path, file.filename or "", client.model)
            job = transfers.start(printer_id, client, artifact, staging=staging)
        except AnycubicError as exc:
            raise HTTPException(400, str(exc)) from None
        handed_off = True
        return job.public()
    finally:
        _receiving.discard(printer_id)
        await file.close()
        if not handed_off:
            path.unlink(missing_ok=True)
            staging.rmdir()


@router.get("/uploads/{job_id}")
async def upload_status(
    printer_id: int,
    job_id: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    await _printer(printer_id)
    job = transfers.get(printer_id, job_id)
    if job is None:
        raise HTTPException(404, "Transfer status unavailable; inspect the printer file list before retrying")
    return job.public()


@router.post("/uploads/{job_id}/cancel")
async def cancel_upload(
    printer_id: int,
    job_id: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
    __=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    await _printer(printer_id)
    job = transfers.get(printer_id, job_id)
    if job is None:
        raise HTTPException(404, "Transfer not found")
    return await transfers.stop(job)

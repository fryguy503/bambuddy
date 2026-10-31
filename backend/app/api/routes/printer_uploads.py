"""Native printer storage uploads; no print dispatch or queue mutation."""

import re
import shutil
from pathlib import Path
from uuid import uuid4

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from backend.app.core.auth import RequirePrinterPermissionIfAuthEnabled
from backend.app.core.config import settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.printers.api import PrinterConnectionAdapter, operation_lease
from backend.app.printers.contracts import PrinterConfig, PrinterPluginError
from backend.app.printers.registry import current_registry, provider_context
from backend.app.services.printer_file_uploads import printer_file_uploads
from backend.app.services.printer_manager import printer_manager

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
DISK_RESERVE_BYTES = 64 * 1024 * 1024


class BoundedUploadRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            if request.method != "POST" or not request.url.path.endswith("/files/upload"):
                return await handler(request)
            maximum = MAX_UPLOAD_BYTES + 1024 * 1024
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) < 0:
                        raise ValueError
                    if int(length) > maximum:
                        raise HTTPException(413, "Printer upload request exceeds the upload limit")
                except ValueError as exc:
                    raise HTTPException(400, "Invalid Content-Length") from exc
            received, exceeded = 0, False

            async def receive():
                nonlocal received, exceeded
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > maximum:
                    exceeded = True
                    # MultipartParser closes all of its partial spooled files.
                    raise MultiPartException("Printer upload request exceeds the upload limit")
                return message

            try:
                return await handler(Request(request.scope, receive=receive))
            except StarletteHTTPException as exc:
                if exceeded:
                    raise HTTPException(413, "Printer upload request exceeds the upload limit") from exc
                raise

        return bounded


router = APIRouter(prefix="/printers", tags=["printer-files"], route_class=BoundedUploadRoute)


def _upload_id(value: str):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise HTTPException(422, "Choose a fresh upload ID with 32 lowercase hexadecimal characters")
    return value


def _filename(value: str | None):
    if (
        not value
        or value in {".", ".."}
        or len(value.encode("utf-8")) > 240
        or any(character in value for character in "/\\:")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise HTTPException(422, "Choose a file with a plain filename")
    return value


@router.post("/{printer_id}/files/upload", status_code=202)
async def upload_printer_file(
    printer_id: int,
    request: Request,
    file: UploadFile = File(...),
    upload_id: str = Form(...),
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    path = None
    owned = False
    reference = None
    reserved = None
    registry = current_registry()
    try:
        filename = _filename(file.filename)
        job_id = _upload_id(upload_id)
        printer = await db.get(Printer, printer_id)
        if printer is None:
            raise HTTPException(404, "Printer not found")
        config = PrinterConfig.from_printer(printer)
        client = printer_manager.get_client(printer_id)
        if not isinstance(client, PrinterConnectionAdapter) or not client.state.connected:
            raise HTTPException(409, "Connect the printer before uploading files")
        if client._config != config:
            raise HTTPException(409, "Printer configuration changed; reconnect before uploading")
        provider = client._plugin
        if "file_upload" not in provider.model_capabilities(config.model):
            raise HTTPException(409, "This printer plugin does not advertise file uploads")
        directory = Path(settings.base_dir) / "tmp" / "printer-uploads"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if file.size == 0:
            raise HTTPException(422, "The selected file is empty")
        if file.size is not None and file.size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Printer file uploads are limited to 512 MiB")
        if shutil.disk_usage(directory).free < (file.size or MAX_UPLOAD_BYTES) + DISK_RESERVE_BYTES:
            raise HTTPException(507, "Not enough free space to stage the printer upload")
        reserved = printer_file_uploads.reserve(printer_id, job_id, filename, file.size or 0)
        path = directory / (uuid4().hex + ".upload")
        reference = "file-receive:" + uuid4().hex
        registry.retain_runtime(provider, reference, printer_id, "file upload in progress")
        with operation_lease(printer_id), provider_context(config, provider):
            size = 0
            async with aiofiles.open(path, "xb") as output:
                while chunk := await file.read(1024 * 1024):
                    if reserved.cancel_requested or await request.is_disconnected():
                        await printer_file_uploads.cancel(printer_id, job_id)
                        raise HTTPException(409, "The upload was cancelled before transfer acceptance")
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Printer file uploads are limited to 512 MiB")
                    await output.write(chunk)
            if not size:
                raise HTTPException(422, "The selected file is empty")
            await db.refresh(printer)
            if PrinterConfig.from_printer(printer) != config:
                raise HTTPException(409, "Printer configuration changed during upload; try again")
            if reserved.cancel_requested or await request.is_disconnected():
                await printer_file_uploads.cancel(printer_id, job_id)
                raise HTTPException(409, "The upload was cancelled before transfer acceptance")
            job = printer_file_uploads.start(config, provider, path, filename, job_id)
            owned = True
            return job.describe()
    except PrinterPluginError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        await file.close()
        if reference is not None:
            registry.release_runtime(reference)
        if path is not None and not owned:
            path.unlink(missing_ok=True)
        if reserved is not None and not owned and reserved.phase == "receiving":
            reserved.phase, reserved.error = "failed", "The file was not accepted for transfer"


@router.get("/{printer_id}/files/uploads/{job_id}")
async def printer_upload_status(
    printer_id: int,
    job_id: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    job = printer_file_uploads.get(printer_id, _upload_id(job_id))
    if job is None:
        raise HTTPException(404, "Printer upload not found")
    return job.describe()


@router.delete("/{printer_id}/files/uploads/{job_id}")
async def cancel_printer_upload(
    printer_id: int,
    job_id: str,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_FILES),
):
    job = await printer_file_uploads.cancel(printer_id, _upload_id(job_id))
    return job.describe()

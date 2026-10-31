"""Core-authorized artifact inspection through the selected printer provider."""

import asyncio
import inspect
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import (
    RequestPrinterScope,
    RequirePrinterPermissionIfAuthEnabled,
    require_ownership_permission,
    security,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import PrinterScope
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.printers.api import operation_lease
from backend.app.printers.contracts import PrinterConfig, PrinterPluginError
from backend.app.printers.registry import plugin_for, provider_context
from backend.app.printers.sdk import JobArtifact, JobDescription, validate_job_description

router = APIRouter(prefix="/printers", tags=["printer-jobs"])


class JobDescriptionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_file_id: int | None = Field(default=None, gt=0)
    archive_id: int | None = Field(default=None, gt=0)
    plate_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_source(self):
        if (self.library_file_id is None) == (self.archive_id is None):
            raise ValueError("Select exactly one library file or archive")
        return self


@router.post("/{printer_id}/job-description", response_model=JobDescription)
async def describe_printer_job(
    printer_id: int,
    body: JobDescriptionSource,
    request: Request,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_READ),
    printer_scope: PrinterScope = RequestPrinterScope,
    db: AsyncSession = Depends(get_db, scope="function"),
):
    from backend.app.api.routes.archives import _ensure_archive_visible
    from backend.app.api.routes.library import _ensure_library_file_visible, _resolve_source_disk_path, to_absolute_path

    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(404, "Printer not found")
    permissions = (
        (Permission.LIBRARY_READ_ALL, Permission.LIBRARY_READ_OWN)
        if body.library_file_id
        else (Permission.ARCHIVES_READ_ALL, Permission.ARCHIVES_READ_OWN)
    )
    actor, read_all = await require_ownership_permission(*permissions)(
        credentials=await security(request), x_api_key=request.headers.get("X-API-Key")
    )
    if body.library_file_id:
        source = _ensure_library_file_visible(
            await db.scalar(LibraryFile.active().where(LibraryFile.id == body.library_file_id)), actor, read_all
        )
        path = _resolve_source_disk_path(source)
        uri = f"library:{source.id}"
        source_format = source.file_type
    else:
        source = _ensure_archive_visible(
            await db.scalar(select(PrintArchive).where(PrintArchive.id == body.archive_id)),
            actor,
            read_all,
            printer_scope,
        )
        path = to_absolute_path(source.file_path)
        uri = f"archive:{source.id}"
        source_format = None
    if path is None or not path.is_file():
        raise HTTPException(404, "Print artifact is unavailable")
    filename = PurePosixPath((source.filename or path.name).replace("\\", "/")).name or path.name
    artifact_format = (source_format or PurePosixPath(filename).suffix.lstrip(".") or "unknown").lower()
    artifact = JobArtifact(
        uri=uri,
        filename=filename,
        format=artifact_format,
        size_bytes=path.stat().st_size,
        metadata={"plate_id": str(body.plate_id)} if body.plate_id is not None else {},
    )
    config = PrinterConfig.from_printer(printer)
    plugin = plugin_for(config)
    with operation_lease(printer_id), provider_context(config, plugin):
        descriptor = plugin.describe_device(config)

        async def describe():
            if inspect.iscoroutinefunction(plugin.describe_job):
                return await plugin.describe_job(config, artifact, path)
            result = await asyncio.to_thread(plugin.describe_job, config, artifact, path)
            return await result if inspect.isawaitable(result) else result

        try:
            result = validate_job_description(descriptor, await asyncio.wait_for(describe(), timeout=60))
        except TimeoutError as exc:
            raise HTTPException(408, "Reading print requirements timed out") from exc
        except PrinterPluginError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "This artifact is not compatible with the selected printer") from exc
    if result.artifact.uri != artifact.uri or result.artifact.filename != artifact.filename:
        raise HTTPException(409, "Plugin returned requirements for a different artifact")
    return result.model_copy(
        update={"inputs": [item.model_copy(update={"artifact_uri": artifact.uri}) for item in result.inputs]}
    )

"""Scoped operator visibility into pending dispatches. No recovery side effects."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePrinterPermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.printers.api import PrinterConnectionAdapter, leased_call
from backend.app.printers.contracts import PrinterConfig, PrinterPluginError
from backend.app.printers.registry import plugin_for
from backend.app.services.printer_manager import printer_manager

from .dispatch_status import DispatchHoldsResponse

router = APIRouter(prefix="/printer-plugins/anycubic", tags=["anycubic-diagnostics"])


@router.get("/printers/{printer_id}/dispatch-holds", response_model=DispatchHoldsResponse)
async def dispatch_holds(
    printer_id: int,
    request: Request,
    _=RequirePrinterPermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    printer = await db.get(Printer, printer_id)
    if printer is None:
        raise HTTPException(404, "Printer not found")
    config = PrinterConfig.from_printer(printer)
    provider = plugin_for(config)
    client = getattr(request.state, "printer_connection", None) or printer_manager.get_client(printer_id)
    if not isinstance(client, PrinterConnectionAdapter):
        raise HTTPException(409, "Printer has no managed connection")
    if provider.id != "anycubic" or client._plugin is not provider or client._config != config:
        raise HTTPException(409, "Printer support or configuration changed; refresh before reading dispatch holds")
    reader = getattr(provider, "dispatch_holds", None)
    if not callable(reader):
        raise HTTPException(409, "The current printer provider does not expose dispatch status")
    try:
        # Lease the exact managed provider while reading its durable metadata.
        holds = await leased_call(config, reader, config, client._connection)
    except PrinterPluginError as exc:
        raise HTTPException(409, "Printer connection does not match its configured provider") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(409, "Dispatch state could not be read; no held operation was changed") from exc
    return DispatchHoldsResponse(printer_id=printer_id, holds=holds)

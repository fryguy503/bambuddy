"""Connection diagnostics delegated to the selected printer addon."""

import asyncio

from pydantic import ValidationError

from backend.app.models.printer import Printer
from backend.app.printers.contracts import PrinterConfig, PrinterPlugin, PrinterPluginError
from backend.app.printers.registry import get_plugin, plugin_for
from backend.app.printers.sdk import SDKValidationError
from backend.app.schemas.printer import PrinterDiagnosticResult

DIAGNOSTIC_TIMEOUT_SECONDS = 45


async def run_connection_diagnostic(
    ip_address: str,
    *,
    printer: Printer | None = None,
    serial_number: str | None = None,
    access_code: str | None = None,
    wait_for_publish_seconds: float = 0.0,
    progress: dict | None = None,
    plugin_id: str = "bambu",
    model: str | None = None,
    plugin_options: dict | None = None,
) -> PrinterDiagnosticResult:
    plugin = plugin_for(printer) if printer is not None else get_plugin(plugin_id)
    config = (
        PrinterConfig.from_printer(printer)
        if printer is not None
        else PrinterConfig(
            ip_address=ip_address,
            serial_number=serial_number or "",
            access_code=access_code or "",
            model=model,
            plugin_id=plugin_id,
            options=plugin_options or {},
        )
    )
    if type(plugin).connection_diagnostics is not PrinterPlugin.connection_diagnostics:
        from backend.app.printers.api import PrinterConnectionAdapter, leased_call
        from backend.app.services.printer_manager import printer_manager

        connection = None
        client = printer_manager.get_client(config.printer_id) if config.printer_id is not None else None
        if isinstance(client, PrinterConnectionAdapter) and client._config == config:
            plugin = client._plugin
            connection = client._connection
        try:
            result = await asyncio.wait_for(
                leased_call(
                    config,
                    plugin.connection_diagnostics,
                    config,
                    connection=connection,
                    wait_for_publish_seconds=wait_for_publish_seconds,
                    progress=progress,
                ),
                timeout=DIAGNOSTIC_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise PrinterPluginError("Connection diagnostics timed out; retry the checks") from exc
    else:
        # Keep installed/bundled providers using the original diagnostic hook
        # compatible without changing their immutable package payloads.
        result = await plugin.diagnostic_operation(
            "connection",
            ip_address,
            printer=printer,
            serial_number=serial_number,
            access_code=access_code,
            wait_for_publish_seconds=wait_for_publish_seconds,
            progress=progress,
        )
    try:
        result = PrinterDiagnosticResult.model_validate(result)
    except ValidationError as exc:
        raise SDKValidationError("Invalid diagnostic result") from exc
    if result.ip_address != config.ip_address or result.printer_id != config.printer_id:
        raise SDKValidationError("Diagnostic result does not identify the requested printer")
    return result

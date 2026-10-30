"""Connection diagnostics delegated to the selected printer addon."""

from backend.app.models.printer import Printer
from backend.app.printers.registry import get_plugin, plugin_for
from backend.app.schemas.printer import PrinterDiagnosticResult


async def run_connection_diagnostic(
    ip_address: str,
    *,
    printer: Printer | None = None,
    serial_number: str | None = None,
    access_code: str | None = None,
    wait_for_publish_seconds: float = 0.0,
    progress: dict | None = None,
    plugin_id: str = "bambu",
) -> PrinterDiagnosticResult:
    plugin = plugin_for(printer) if printer is not None else get_plugin(plugin_id)
    return await plugin.diagnostic_operation(
        "connection",
        ip_address,
        printer=printer,
        serial_number=serial_number,
        access_code=access_code,
        wait_for_publish_seconds=wait_for_publish_seconds,
        progress=progress,
    )

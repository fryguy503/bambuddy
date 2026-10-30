"""Internal printer addon API; protocol implementations live in bundled plugins."""

from backend.app.printers.contracts import (
    PRINTER_API_VERSION,
    PrinterConfig,
    PrinterConnection,
    PrinterModel,
    PrinterPlugin,
    PrinterPluginError,
    UnknownPrinterPlugin,
    UnsupportedPrinterOperation,
)
from backend.app.printers.registry import get_plugin, list_plugins, plugin_for, register_plugin

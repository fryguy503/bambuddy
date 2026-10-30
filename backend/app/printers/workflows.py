"""Select workflow policy using the managed device's provider identity."""

from backend.app.printers.api import config_for_id
from backend.app.printers.registry import get_plugin, plugin_for


def provider_for_id(printer_id):
    config = config_for_id(printer_id)
    if config is not None:
        return plugin_for(config)
    from backend.app.services.printer_manager import printer_manager

    printer = printer_manager.get_printer(printer_id)
    return plugin_for(printer) if printer is not None else get_plugin("bambu")

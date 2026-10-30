"""Select workflow policy using the managed device's provider identity."""

from backend.app.printers.api import config_for_id
from backend.app.printers.registry import get_plugin


def provider_for_id(printer_id):
    config = config_for_id(printer_id)
    if config is not None:
        return get_plugin(config.plugin_id)
    from backend.app.services.printer_manager import printer_manager

    printer = printer_manager.get_printer(printer_id)
    plugin_id = getattr(printer, "plugin_id", None)
    return get_plugin(plugin_id if isinstance(plugin_id, str) and plugin_id else "bambu")

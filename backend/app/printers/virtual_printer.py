"""Access slicer-facing printer emulation through the installed addon contract."""

from typing import Any

from backend.app.printers.registry import get_plugin


def get_virtual_printer_service(plugin_id: str = "bambu") -> Any:
    """Return the selected plugin's service; the bundled Bambu service is default."""
    return get_plugin(plugin_id).virtual_printer_service()

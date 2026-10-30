"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from importlib import import_module as _import_module
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer")

# Prebind submodule aliases before replacing the package. Otherwise Python
# could load a second manager under the legacy package name.
for _name in (
    "_debug",
    "bind_server",
    "certificate",
    "diagnostic",
    "ftp_server",
    "manager",
    "mqtt_bridge",
    "mqtt_server",
    "ssdp_server",
    "tailscale",
    "tcp_proxy",
):
    sys.modules[f"{__name__}.{_name}"] = _import_module(f"{_implementation.__name__}.{_name}")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer import (
        DEFAULT_VIRTUAL_PRINTER_MODEL as DEFAULT_VIRTUAL_PRINTER_MODEL,
        VIRTUAL_PRINTER_MODELS as VIRTUAL_PRINTER_MODELS,
        __all__ as __all__,
        virtual_printer_manager as virtual_printer_manager,
    )

sys.modules[__name__] = _implementation

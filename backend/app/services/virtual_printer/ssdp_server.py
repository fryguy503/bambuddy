"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.ssdp_server`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.ssdp_server")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.ssdp_server import (
        BAMBU_SEARCH_TARGET as BAMBU_SEARCH_TARGET,
        SSDP_BROADCAST_ADDR as SSDP_BROADCAST_ADDR,
        SSDP_MULTICAST_ADDR as SSDP_MULTICAST_ADDR,
        SSDP_PORT as SSDP_PORT,
        SSDPProxy as SSDPProxy,
        VirtualPrinterSSDPServer as VirtualPrinterSSDPServer,
        asyncio as asyncio,
        logger as logger,
        logging as logging,
        re as re,
        socket as socket,
        struct as struct,
    )

sys.modules[__name__] = _implementation

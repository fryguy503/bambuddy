"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.bind_server`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.bind_server")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.bind_server import (
        BIND_PORT_PLAIN as BIND_PORT_PLAIN,
        BIND_PORT_TLS as BIND_PORT_TLS,
        BIND_PORTS as BIND_PORTS,
        FRAME_HEADER as FRAME_HEADER,
        FRAME_TRAILER as FRAME_TRAILER,
        HEADER_SIZE as HEADER_SIZE,
        TRAILER_SIZE as TRAILER_SIZE,
        BindServer as BindServer,
        Path as Path,
        asyncio as asyncio,
        json as json,
        logger as logger,
        logging as logging,
        ssl as ssl,
        struct as struct,
    )

sys.modules[__name__] = _implementation

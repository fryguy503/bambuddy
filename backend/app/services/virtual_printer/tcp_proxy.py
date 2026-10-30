"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.tcp_proxy`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.tcp_proxy")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.tcp_proxy import (
        Callable as Callable,
        Path as Path,
        SlicerProxyManager as SlicerProxyManager,
        TCPProxy as TCPProxy,
        TLSProxy as TLSProxy,
        asyncio as asyncio,
        detect_port_redirect as detect_port_redirect,
        logger as logger,
        logging as logging,
        re as re,
        ssl as ssl,
        subprocess as subprocess,
    )

sys.modules[__name__] = _implementation

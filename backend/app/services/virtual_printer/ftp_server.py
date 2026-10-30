"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.ftp_server`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.ftp_server")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.ftp_server import (
        FTP_PORT as FTP_PORT,
        MAX_UPLOAD_BYTES as MAX_UPLOAD_BYTES,
        PASSIVE_MAX_SLOTS as PASSIVE_MAX_SLOTS,
        PASSIVE_PORT_BASE as PASSIVE_PORT_BASE,
        PASSIVE_SLICE_SIZE as PASSIVE_SLICE_SIZE,
        Callable as Callable,
        FTPSession as FTPSession,
        Path as Path,
        VirtualPrinterFTPServer as VirtualPrinterFTPServer,
        asyncio as asyncio,
        compute_passive_port_slice as compute_passive_port_slice,
        hmac as hmac,
        logger as logger,
        logging as logging,
        os as os,
        random as random,
        ssl as ssl,
        zipfile as zipfile,
    )

sys.modules[__name__] = _implementation

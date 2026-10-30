"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.tailscale`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.tailscale")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.tailscale import (
        _SUBPROCESS_ENV as _SUBPROCESS_ENV,
        Path as Path,
        TailscaleService as TailscaleService,
        TailscaleStatus as TailscaleStatus,
        asyncio as asyncio,
        dataclass as dataclass,
        field as field,
        json as json,
        logger as logger,
        logging as logging,
        os as os,
        shutil as shutil,
        tailscale_service as tailscale_service,
    )

sys.modules[__name__] = _implementation

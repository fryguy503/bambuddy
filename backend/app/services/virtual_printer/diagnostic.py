"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.diagnostic`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.diagnostic")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.diagnostic import (
        _CAP_NET_BIND_SERVICE as _CAP_NET_BIND_SERVICE,
        _PORT_PROBE_TIMEOUT as _PORT_PROBE_TIMEOUT,
        PORT_BIND as PORT_BIND,
        PORT_BIND_PLAIN as PORT_BIND_PLAIN,
        PORT_FTPS as PORT_FTPS,
        PORT_MQTT as PORT_MQTT,
        DiagnosticCheck as DiagnosticCheck,
        VirtualPrinter as VirtualPrinter,
        VPDiagnosticResult as VPDiagnosticResult,
        _check_port as _check_port,
        asyncio as asyncio,
        can_bind_privileged_ports as can_bind_privileged_ports,
        logger as logger,
        logging as logging,
        os as os,
        run_vp_diagnostic as run_vp_diagnostic,
    )

sys.modules[__name__] = _implementation

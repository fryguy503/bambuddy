"""Compatibility alias for :mod:`backend.app.printers.bambu.hms_actions`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "hms_actions")

if TYPE_CHECKING:
    from backend.app.printers.bambu.hms_actions import (
        HMSAction as HMSAction,
    )

sys.modules[__name__] = _implementation

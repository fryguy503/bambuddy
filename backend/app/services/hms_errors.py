"""Compatibility alias for :mod:`backend.app.printers.bambu.hms_errors`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "hms_errors")

if TYPE_CHECKING:
    from backend.app.printers.bambu.hms_errors import (
        get_error_description as get_error_description,
    )

sys.modules[__name__] = _implementation

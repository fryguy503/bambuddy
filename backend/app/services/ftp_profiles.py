"""Compatibility alias for :mod:`backend.app.printers.bambu.ftp_profiles`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "ftp_profiles")

sys.modules[__name__] = _implementation

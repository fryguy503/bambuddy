"""Compatibility alias for the bundled Bambu printer plugin's camera_diagnose."""

import sys

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "camera_diagnose")

sys.modules[__name__] = _implementation

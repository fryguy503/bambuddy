"""Compatibility alias for :mod:`backend.app.printers.bambu.camera_profiles`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "camera_profiles")

if TYPE_CHECKING:
    from backend.app.printers.bambu.camera_profiles import (
        _MODEL_ALIASES as _MODEL_ALIASES,
        _PROFILES as _PROFILES,
        DEFAULT_PROFILE as DEFAULT_PROFILE,
        get_camera_profile as get_camera_profile,
    )

sys.modules[__name__] = _implementation

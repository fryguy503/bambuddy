"""Deprecated import path for the active printer plugin compatibility API."""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

if TYPE_CHECKING:
    from backend.app.printers.bambu.cloud_routes import *  # noqa: F403
    from backend.app.printers.bambu.cloud_routes import (
        _BUILTIN_FILAMENT_NAMES as _BUILTIN_FILAMENT_NAMES,
        _cloud_api_key_gate as _cloud_api_key_gate,
    )

_implementation = compatibility_module("bambu", "cloud_routes")
sys.modules[__name__] = _implementation

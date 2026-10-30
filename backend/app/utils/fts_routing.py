"""Deprecated import path for the active printer plugin compatibility API."""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

if TYPE_CHECKING:
    from backend.app.printers.bambu.fts_routing import *  # noqa: F403

_implementation = compatibility_module("bambu", "fts_routing")
sys.modules[__name__] = _implementation

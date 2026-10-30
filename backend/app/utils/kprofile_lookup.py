"""Deprecated import path for the active printer plugin compatibility API."""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

if TYPE_CHECKING:
    from backend.app.printers.bambu.kprofile_lookup import *  # noqa: F403

_implementation = compatibility_module("bambu", "kprofile_lookup")
sys.modules[__name__] = _implementation

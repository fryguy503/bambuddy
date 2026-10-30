"""Compatibility alias for the bundled Bambu printer plugin's diagnostic."""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "diagnostic")

if TYPE_CHECKING:
    from backend.app.printers.bambu.diagnostic import (
        PUBLISH_WAIT_DEFAULT as PUBLISH_WAIT_DEFAULT,
        _detect_container_network_mode as _detect_container_network_mode,
    )

sys.modules[__name__] = _implementation

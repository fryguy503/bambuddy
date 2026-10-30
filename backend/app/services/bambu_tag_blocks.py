"""Compatibility alias for :mod:`backend.app.printers.bambu.tag_blocks`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "tag_blocks")

if TYPE_CHECKING:
    from backend.app.printers.bambu.tag_blocks import (
        spool_prefill_from_tag as spool_prefill_from_tag,
    )

sys.modules[__name__] = _implementation

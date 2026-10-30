"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer._debug`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer._debug")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer._debug import (
        _ENV_FLAG as _ENV_FLAG,
        _NAME_SAFE as _NAME_SAFE,
        _command_label as _command_label,
        _enabled as _enabled,
        _sanitize as _sanitize,
        annotations as annotations,
        app_settings as app_settings,
        append_event as append_event,
        datetime as datetime,
        dump_wire as dump_wire,
        json as json,
        logger as logger,
        logging as logging,
        os as os,
        re as re,
        timezone as timezone,
    )

sys.modules[__name__] = _implementation

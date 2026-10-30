"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.mqtt_server`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.mqtt_server")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.mqtt_server import (
        _AUTH_RATE_LIMIT_MAX_ATTEMPTS as _AUTH_RATE_LIMIT_MAX_ATTEMPTS,
        _AUTH_RATE_LIMIT_WINDOW_SECONDS as _AUTH_RATE_LIMIT_WINDOW_SECONDS,
        _COMMANDS_LOGGED_WHOLE as _COMMANDS_LOGGED_WHOLE,
        _MIRRORED_PRINT_STATES as _MIRRORED_PRINT_STATES,
        _PENDING_REQUEST_MAX_ENTRIES as _PENDING_REQUEST_MAX_ENTRIES,
        _UPLOAD_SETTLE_SECONDS as _UPLOAD_SETTLE_SECONDS,
        MODEL_PRODUCT_NAMES as MODEL_PRODUCT_NAMES,
        MQTT_PORT as MQTT_PORT,
        Callable as Callable,
        Path as Path,
        SimpleMQTTServer as SimpleMQTTServer,
        append_event as append_event,
        asyncio as asyncio,
        copy as copy,
        dump_wire as dump_wire,
        hmac as hmac,
        json as json,
        logger as logger,
        logging as logging,
        socket as socket,
        ssl as ssl,
        time as time,
    )

sys.modules[__name__] = _implementation

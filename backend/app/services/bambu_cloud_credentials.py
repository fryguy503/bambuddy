"""Compatibility alias for :mod:`backend.app.printers.bambu.cloud_credentials`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "cloud_credentials")

if TYPE_CHECKING:
    from backend.app.printers.bambu.cloud_credentials import (
        CLOUD_EMAIL_KEY as CLOUD_EMAIL_KEY,
        CLOUD_REGION_KEY as CLOUD_REGION_KEY,
        CLOUD_TOKEN_INVALID_KEY as CLOUD_TOKEN_INVALID_KEY,
        CLOUD_TOKEN_KEY as CLOUD_TOKEN_KEY,
        _clear_cloud_token_invalid as _clear_cloud_token_invalid,
        _normalise_region as _normalise_region,
        get_stored_token as get_stored_token,
        is_cloud_token_invalid as is_cloud_token_invalid,
        mark_cloud_token_invalid as mark_cloud_token_invalid,
    )

sys.modules[__name__] = _implementation

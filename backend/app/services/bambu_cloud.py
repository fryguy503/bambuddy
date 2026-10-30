"""Compatibility alias for :mod:`backend.app.printers.bambu.cloud`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "cloud")

if TYPE_CHECKING:
    from backend.app.printers.bambu.cloud import (
        _SLICER_API_VERSION as _SLICER_API_VERSION,
        BambuCloudAuthError as BambuCloudAuthError,
        BambuCloudError as BambuCloudError,
        BambuCloudService as BambuCloudService,
        invalidate_validation_cache as invalidate_validation_cache,
        is_captcha_challenge as is_captcha_challenge,
        is_expiry_401 as is_expiry_401,
        set_shared_http_client as set_shared_http_client,
    )

sys.modules[__name__] = _implementation

"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.certificate`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.certificate")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.certificate import (
        CA_COMMON_NAME_PREFIX as CA_COMMON_NAME_PREFIX,
        CA_EXPIRY_THRESHOLD_DAYS as CA_EXPIRY_THRESHOLD_DAYS,
        DEFAULT_SERIAL as DEFAULT_SERIAL,
        CertificateService as CertificateService,
        ExtendedKeyUsageOID as ExtendedKeyUsageOID,
        IPv4Address as IPv4Address,
        NameOID as NameOID,
        Path as Path,
        _get_local_ip as _get_local_ip,
        datetime as datetime,
        hashes as hashes,
        logger as logger,
        logging as logging,
        rsa as rsa,
        serialization as serialization,
        socket as socket,
        timedelta as timedelta,
        timezone as timezone,
        x509 as x509,
    )

sys.modules[__name__] = _implementation

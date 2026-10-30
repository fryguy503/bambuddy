"""Compatibility alias for :mod:`backend.app.printers.bambu.virtual_printer.mqtt_bridge`.

The bundled Bambu plugin owns the implementation. Both import paths share
one module object, caches, singleton services and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "virtual_printer.mqtt_bridge")

if TYPE_CHECKING:
    from backend.app.printers.bambu.virtual_printer.mqtt_bridge import (
        ADVERTISE_ADDRESS_ENV as ADVERTISE_ADDRESS_ENV,
        REFRESH_INTERVAL_SECONDS as REFRESH_INTERVAL_SECONDS,
        MQTTBridge as MQTTBridge,
        _ip_to_uint32_le as _ip_to_uint32_le,
        _merge_ams_dict as _merge_ams_dict,
        _resolve_advertise_override as _resolve_advertise_override,
        _resolve_host_interface_for_target as _resolve_host_interface_for_target,
        _resolve_target_to_ipv4 as _resolve_target_to_ipv4,
        annotations as annotations,
        append_event as append_event,
        apply_tray_exist_bits as apply_tray_exist_bits,
        asyncio as asyncio,
        copy as copy,
        dump_wire as dump_wire,
        ipaddress as ipaddress,
        json as json,
        logger as logger,
        logging as logging,
        os as os,
        socket as socket,
        unit_reported_absent as unit_reported_absent,
    )

sys.modules[__name__] = _implementation

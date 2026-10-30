"""Compatibility alias for :mod:`backend.app.printers.bambu.mqtt`.

The bundled Bambu plugin owns this implementation. Keep one module object so
legacy import paths share caches, singleton services, and patched attributes.
"""

import sys
from typing import TYPE_CHECKING

from backend.app.printers.compat import compatibility_module

_implementation = compatibility_module("bambu", "mqtt")

if TYPE_CHECKING:
    from backend.app.printers.bambu.mqtt import (
        _RACK_NOZZLE_IDS as _RACK_NOZZLE_IDS,
        CALI_ACK_UNCONFIRMED as CALI_ACK_UNCONFIRMED,
        HMS_MQTT_VERIFY_FAILED as HMS_MQTT_VERIFY_FAILED,
        BambuMQTTClient as BambuMQTTClient,
        KProfileReadError as KProfileReadError,
        apply_tray_exist_bits as apply_tray_exist_bits,
        get_stage_name as get_stage_name,
        resolve_external_spools_in_mapping as resolve_external_spools_in_mapping,
        resolve_rack_plan_mapping as resolve_rack_plan_mapping,
        unit_reported_absent as unit_reported_absent,
    )

sys.modules[__name__] = _implementation

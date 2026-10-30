"""Neutral SDK telemetry plus deprecated legacy type import compatibility.

API2 drivers use DeviceSnapshot and stable resource identities. The legacy
PrinterState/HMS/AMS types are owned by the selected Bambu plugin package;
this module contains no vendor wire schema or hardware limits.
"""

from typing import TYPE_CHECKING, Any

from backend.app.printers.sdk.domain import (
    DeviceEvent as DeviceEvent,
    DeviceSnapshot as DeviceSnapshot,
    ResourceState as ResourceState,
    TelemetryValue as TelemetryValue,
)

if TYPE_CHECKING:
    from backend.app.printers.bambu.legacy_state import (
        DryingCycleEnd as DryingCycleEnd,
        ExtruderSlot as ExtruderSlot,
        FilaSwitchState as FilaSwitchState,
        HMSError as HMSError,
        KProfile as KProfile,
        MQTTLogEntry as MQTTLogEntry,
        NozzleInfo as NozzleInfo,
        PrinterState as PrinterState,
        PrintOptions as PrintOptions,
    )

_LEGACY_TYPES = frozenset(
    (
        "DryingCycleEnd",
        "MQTTLogEntry",
        "HMSError",
        "KProfile",
        "NozzleInfo",
        "FilaSwitchState",
        "ExtruderSlot",
        "PrintOptions",
        "PrinterState",
    )
)


def __getattr__(name: str) -> Any:
    if name not in _LEGACY_TYPES:
        raise AttributeError(name)
    from backend.app.printers.compat import compatibility_module

    return getattr(compatibility_module("bambu", "legacy_state"), name)

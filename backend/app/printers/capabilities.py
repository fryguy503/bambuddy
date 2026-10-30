"""Feature guards resolved from plugin metadata, with bundled firmware rules."""

from __future__ import annotations

from backend.app.printers.models import _provider


def _feature(
    model: str | None, method: str, capability: str, plugin_id: str | None = None, firmware: str | None = None
) -> bool:
    plugin = _provider(model, plugin_id)
    return bool(plugin.feature_operation(method, model, firmware=firmware))


def supports_chamber_temp(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "supports_chamber_temp", "chamber_temperature", plugin_id)


def supports_chamber_heater(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "supports_chamber_heater", "chamber_heater", plugin_id)


def supports_airduct(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "supports_airduct", "airduct", plugin_id)


def has_stg_cur_idle_bug(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "has_stg_cur_idle_bug", "stage_idle_workaround", plugin_id)


def drying_screen_only(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "drying_screen_only", "drying_screen_only", plugin_id)


def supports_drying(model: str | None, firmware: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "supports_drying", "drying", plugin_id, firmware)


def supports_drying_while_printing(model: str | None, firmware: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "supports_drying_while_printing", "drying_while_printing", plugin_id, firmware)


def display_temperatures(
    temperatures: dict | None, model: str | None, *, plugin_id: str | None = None
) -> dict[str, float]:
    plugin = _provider(model, plugin_id)
    return plugin.feature_operation("display_temperatures", model, temperatures=temperatures)

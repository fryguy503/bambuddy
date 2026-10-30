"""Model identity and capability dispatch through installed printer plugins.

Existing model-only callers resolve unique catalog aliases. Callers that know
the printer should pass ``plugin_id`` to disambiguate names shared by vendors.
"""

from __future__ import annotations

from backend.app.printers.contracts import PrinterPluginError
from backend.app.printers.registry import get_plugin, list_plugins


def _provider(model: str | None, plugin_id: str | None = None):
    if plugin_id is not None:
        return get_plugin(plugin_id)
    matches = [plugin for plugin in list_plugins() if plugin.model(model) is not None]
    if len(matches) > 1:
        raise PrinterPluginError("Model name belongs to multiple printer plugins; pass plugin_id")
    return matches[0] if matches else get_plugin("bambu")


def model_capabilities(model: str | None, *, plugin_id: str | None = None) -> frozenset[str]:
    return _provider(model, plugin_id).model_capabilities(model)


def normalize_printer_model(raw_model: str | None, *, plugin_id: str | None = None) -> str | None:
    if not raw_model:
        return None
    plugin = _provider(raw_model, plugin_id)
    return plugin.model_operation("normalize_printer_model", raw_model)


def normalize_printer_model_id(model_id: str | None, *, plugin_id: str | None = None) -> str | None:
    if not model_id:
        return None
    plugin = _provider(model_id, plugin_id)
    return plugin.model_operation("normalize_printer_model_id", model_id)


def is_gcode_compatible(
    sliced_for_model: str | None, target_model: str | None, *, plugin_id: str | None = None
) -> bool:
    if not sliced_for_model or not target_model:
        return True
    source = _provider(sliced_for_model, plugin_id)
    target = _provider(target_model, plugin_id)
    if source.id != target.id:
        return False
    return bool(source.model_operation("is_gcode_compatible", sliced_for_model, target_model))


def _feature(model: str | None, method: str, capability: str, plugin_id: str | None = None) -> bool:
    plugin = _provider(model, plugin_id)
    return bool(plugin.model_operation(method, model))


def uses_exhaust_fan_label(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "uses_exhaust_fan_label", "exhaust_fan", plugin_id)


def aux_fan_sides(model: str | None, *, plugin_id: str | None = None) -> tuple[str, str]:
    return _provider(model, plugin_id).model_operation("aux_fan_sides", model)


def has_ethernet(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "has_ethernet", "ethernet", plugin_id)


def has_external_storage(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "has_external_storage", "external_storage", plugin_id)


def has_remote_storage_toggle(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "has_remote_storage_toggle", "remote_storage_toggle", plugin_id)


def is_dual_nozzle_model(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "is_dual_nozzle_model", "dual_nozzle", plugin_id)


def is_nozzle_rack_model(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "is_nozzle_rack_model", "nozzle_rack", plugin_id)


def supports_nozzle_flow_type(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "supports_nozzle_flow_type", "nozzle_flow_type", plugin_id)


def uses_legacy_rfid_refresh(model: str | None, *, plugin_id: str | None = None) -> bool:
    return _feature(model, "uses_legacy_rfid_refresh", "legacy_rfid_refresh", plugin_id)


def get_rod_type(model: str | None, *, plugin_id: str | None = None) -> str | None:
    return _provider(model, plugin_id).model_operation("get_rod_type", model)


# Legacy constants are read-only bundled-adapter data. Runtime core decisions
# must use the functions above so installed plugin catalogs participate.
_LEGACY_CONSTANTS = frozenset(
    {
        "MAX_CHAMBER_TEMP_C",
        "PRINTER_MODEL_MAP",
        "PRINTER_MODEL_ID_MAP",
        "CARBON_ROD_MODELS",
        "STEEL_ROD_MODELS",
        "LINEAR_RAIL_MODELS",
        "SINGLE_NOZZLE_FLOW_MODELS",
        "NO_EXTERNAL_STORAGE_MODELS",
        "NO_REMOTE_STORAGE_TOGGLE_MODELS",
        "ETHERNET_MODELS",
        "DUAL_NOZZLE_MODELS",
        "NOZZLE_RACK_MODELS",
        "EXHAUST_FAN_LABEL_MODELS",
        "AUX_FANS_LEFT_FIRST_MODELS",
        "LEGACY_RFID_REFRESH_MODELS",
        "GCODE_COMPAT_FAMILIES",
    }
)


def __getattr__(name: str):
    if name not in _LEGACY_CONSTANTS:
        raise AttributeError(name)
    return get_plugin().model_operation("constant", name)

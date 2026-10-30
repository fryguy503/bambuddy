"""Normalized telemetry access for core workflows.

The historical keys ``ams`` and ``vt_tray`` are retained as shared API names.
Their values are normalized filament units and external feeds; core never
needs to inspect a vendor's raw protocol payload to obtain them.
"""

from __future__ import annotations

from backend.app.printers.api import config_for_id
from backend.app.printers.models import _provider
from backend.app.printers.registry import get_plugin


def printer_telemetry(
    state, *, printer_id: int | None = None, plugin_id: str | None = None, model: str | None = None
) -> dict:
    if state is None:
        return {}
    if plugin_id is None:
        configured = config_for_id(printer_id)
        if configured is not None:
            plugin_id = configured.plugin_id
    if plugin_id is None:
        state_plugin_id = getattr(state, "plugin_id", None)
        if isinstance(state_plugin_id, str) and state_plugin_id:
            plugin_id = state_plugin_id
    plugin = get_plugin(plugin_id) if plugin_id else _provider(model)
    telemetry = plugin.normalize_telemetry(state)
    return telemetry if isinstance(telemetry, dict) else {}


def filament_units(state, **context) -> list[dict]:
    units = printer_telemetry(state, **context).get("ams")
    return units if isinstance(units, list) else []


def loaded_materials(state, *, plugin_id: str | None = None) -> list[dict]:
    """Let the selected provider project its inventory for host workflows."""
    if state is None:
        return []
    state_plugin_id = getattr(state, "plugin_id", None)
    if plugin_id is None and isinstance(state_plugin_id, str) and state_plugin_id:
        plugin_id = state_plugin_id
    plugin = get_plugin(plugin_id) if plugin_id else _provider(None)
    return plugin.loaded_materials(state)


def external_spools(state, **context) -> list[dict]:
    feeds = printer_telemetry(state, **context).get("vt_tray")
    return feeds if isinstance(feeds, list) else []


def normalized_state_key(state) -> str:
    """Fields filled by generic adapters that the legacy status key predates."""
    values = []
    for field in ("status_name", "filament_units", "external_spools", "active_plate_id"):
        value = getattr(state, field, None)
        values.append(value if isinstance(value, (str, int, list, dict)) else None)
    from backend.app.printers.sdk import DeviceSnapshot

    snapshot = getattr(state, "device_snapshot", None)
    if isinstance(snapshot, DeviceSnapshot):
        data = snapshot.model_dump(mode="json")
        # Heartbeats and refreshed freshness windows alone need no broadcast.
        # Quality, values, units, resource identity and arbitrary extensions do.
        for name in ("observed_at", "sequence"):
            data.pop(name, None)
        for resource in data.get("resources", []):
            for telemetry in resource.get("properties", {}).values():
                telemetry.pop("observed_at", None)
                telemetry.pop("valid_until", None)
        values.append(data)
    return repr(values)

"""Keep accepted work bound to the provider that received it during refreshes."""

from backend.app.printers.contracts import PrinterConfig, PrinterPluginError
from backend.app.printers.management import printer_pin_identity
from backend.app.printers.registry import current_registry, plugin_for

PROVIDER_BINDING = "bambuddy:provider"


def bind_operation_schema(schema: dict, config: PrinterConfig, plugin) -> dict:
    registry = current_registry()
    candidate = registry.runtime_for(plugin)
    return {
        **schema,
        "extensions": {
            **schema.get("extensions", {}),
            PROVIDER_BINDING: {
                "plugin_id": config.plugin_id,
                "version": candidate.version,
                "identity": printer_pin_identity(config),
            },
        },
    }


def operation_provider(row, config: PrinterConfig):
    registry = current_registry()
    binding = row.operation_schema.get("extensions", {}).get(PROVIDER_BINDING)
    if binding is None:
        plugin = plugin_for(config)
    else:
        if (
            not isinstance(binding, dict)
            or binding.get("plugin_id") != config.plugin_id
            or binding.get("identity") != printer_pin_identity(config)
            or not isinstance(binding.get("version"), str)
        ):
            raise PrinterPluginError("Operation belongs to a different printer/provider identity")
        plugin = registry.prepare(config.plugin_id, binding["version"]).plugin
    if row.receipt.get("status") in {"accepted", "running"}:
        registry.retain_runtime(plugin, "operation:" + row.id, config.printer_id, "pending operation")
    return plugin


def operation_connection(printer_id: int, plugin):
    from backend.app.services.printer_manager import printer_manager

    current = printer_manager.get_client(printer_id)
    if current is not None and getattr(current, "_plugin", None) is plugin:
        return getattr(current, "_connection", current)
    retained = printer_manager.retained_connection(printer_id, plugin)
    if retained is None:
        raise PrinterPluginError("The original operation connection is unavailable; its outcome must be reconciled")
    return getattr(retained, "_connection", retained)


def release_terminal_operation(row) -> None:
    if row.receipt.get("status") in {"succeeded", "failed", "cancelled"}:
        current_registry().release_runtime("operation:" + row.id)

"""Core printer services used by bundled and installed addons.

This is an in-process API. HTTP entry points apply the same authentication,
printer scopes and permissions as the rest of Bambuddy before calling it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.app.printers.contracts import PrinterConfig, PrinterPluginError, UnsupportedPrinterOperation
from backend.app.printers.registry import get_plugin, list_plugins

if TYPE_CHECKING:
    from backend.app.printers.sdk.host import HostEventBridge
    from backend.app.printers.state import PrinterState

_bindings: dict[int, PrinterConfig] = {}


def bind_printer(printer: Any) -> None:
    config = PrinterConfig.from_printer(printer)
    # Retain routing for disabled/offline printers and missing addons. A later
    # lookup must fail closed instead of falling back to another vendor.
    if config.printer_id is not None:
        _bindings[config.printer_id] = config


def unbind_printer(printer_id: int) -> None:
    _bindings.pop(printer_id, None)


def config_for_id(printer_id: int | None) -> PrinterConfig | None:
    return _bindings.get(printer_id) if printer_id is not None else None


def config_for_ip(
    ip_address: str,
    *,
    plugin_id: str | None = None,
    printer_id: int | None = None,
) -> PrinterConfig | None:
    configs = [config for config in _bindings.values() if config.ip_address == ip_address]
    if printer_id is not None:
        config = config_for_id(printer_id)
        if config is not None:
            if config.ip_address != ip_address or (plugin_id is not None and config.plugin_id != plugin_id):
                raise PrinterPluginError("Printer id does not match the requested address or plugin")
            return config
        if configs:
            raise PrinterPluginError("Printer id has no binding for the requested address")
        # Historical direct calls may use an unbound address. No saved
        # configuration is selected in that case; the legacy provider decides.
        return None
    if plugin_id is not None:
        configs = [config for config in configs if config.plugin_id == plugin_id]
    if len(configs) > 1:
        raise PrinterPluginError(
            "Multiple printer plugins or configurations share this address; pass printer_id explicitly"
        )
    return configs[0] if configs else None


def plugin_for_ip(ip_address: str, *, plugin_id: str | None = None, printer_id: int | None = None):
    config = config_for_ip(ip_address, plugin_id=plugin_id, printer_id=printer_id)
    return get_plugin(plugin_id or (config.plugin_id if config else "bambu"))


class PrinterAPI:
    version = 2
    legacy_version = 1

    def describe_device(self, printer: Any):
        from backend.app.printers.sdk import validate_device

        config = PrinterConfig.from_printer(printer)
        return validate_device(get_plugin(config.plugin_id).describe_device(config))

    def snapshot_device(self, printer: Any, connection: Any):
        from backend.app.printers.sdk import validate_snapshot

        config = PrinterConfig.from_printer(printer)
        if isinstance(connection, PrinterConnectionAdapter) and connection._event_bridge is not None:
            snapshot = connection._event_bridge.latest_snapshot
            if snapshot is not None:
                return validate_snapshot(self.describe_device(config), snapshot)
        raw = connection._connection if isinstance(connection, PrinterConnectionAdapter) else connection
        return validate_snapshot(
            self.describe_device(config), get_plugin(config.plugin_id).snapshot_device(config, raw)
        )

    async def invoke(self, printer: Any, connection: Any, request):
        import asyncio
        import inspect

        from backend.app.printers.sdk import OperationRequest, validate_invocation, validate_receipt

        config = PrinterConfig.from_printer(printer)
        request = request if isinstance(request, OperationRequest) else OperationRequest.model_validate(request)
        descriptor = self.describe_device(config)
        operation = validate_invocation(descriptor, request)
        raw = connection._connection if isinstance(connection, PrinterConnectionAdapter) else connection
        plugin = get_plugin(config.plugin_id)

        async def perform():
            if inspect.iscoroutinefunction(plugin.invoke):
                return await plugin.invoke(config, raw, request)
            result = await asyncio.to_thread(plugin.invoke, config, raw, request)
            return await result if inspect.isawaitable(result) else result

        receipt = await asyncio.wait_for(perform(), timeout=operation.timeout_seconds)
        return validate_receipt(operation, request, receipt)

    def create_connection(self, printer: Any, **callbacks: Any):
        config = PrinterConfig.from_printer(printer)
        plugin = get_plugin(config.plugin_id)
        plugin.validate_config(config)
        from backend.app.printers.sdk.host import adapt_callbacks

        adapted = adapt_callbacks(config, plugin, callbacks)
        connection = plugin.create_connection(config, **adapted)
        bind_printer(config)
        adapter = wrap_connection(config, connection)
        adapter._event_bridge = adapted.event_bridge
        return adapter

    def capabilities(self, printer: Any) -> dict:
        config = PrinterConfig.from_printer(printer)
        plugin = get_plugin(config.plugin_id)
        model = plugin.model(config.model)
        return {
            "plugin_id": plugin.id,
            "model": model.to_dict() if model else None,
            "capabilities": sorted(plugin.model_capabilities(config.model)),
            "operations": list(plugin.operations),
        }

    def execute(self, printer: Any, connection: Any, operation: str, args: dict | None = None):
        config = PrinterConfig.from_printer(printer)
        raw = connection._connection if isinstance(connection, PrinterConnectionAdapter) else connection
        return get_plugin(config.plugin_id).execute(config, raw, operation, args or {})

    def files(self, printer: Any, operation: str, *args: Any, **kwargs: Any):
        config = PrinterConfig.from_printer(printer)
        return get_plugin(config.plugin_id).file_operation_for(config, operation, *args, **kwargs)

    def camera(self, printer: Any, operation: str, *args: Any, **kwargs: Any):
        config = PrinterConfig.from_printer(printer)
        return get_plugin(config.plugin_id).camera_operation_for(config, operation, *args, **kwargs)

    def serialize_state(self, printer: Any, state: Any, drying_targets: dict | None = None) -> dict:
        from backend.app.printers.serialization import printer_state_to_dict

        config = PrinterConfig.from_printer(printer)
        return printer_state_to_dict(
            state,
            config.printer_id,
            config.model,
            drying_targets,
            plugin_id=config.plugin_id,
        )

    def discovery(self, plugin_id: str, operation: str, *args: Any, **kwargs: Any):
        return get_plugin(plugin_id).discovery_operation(operation, *args, **kwargs)

    def firmware(self, printer: Any, operation: str, *args: Any, **kwargs: Any):
        config = PrinterConfig.from_printer(printer)
        return get_plugin(config.plugin_id).firmware_operation(operation, *args, **kwargs)

    def diagnostic(self, printer: Any, operation: str, *args: Any, **kwargs: Any):
        config = PrinterConfig.from_printer(printer)
        return get_plugin(config.plugin_id).diagnostic_operation(operation, *args, **kwargs)

    def cloud(self, plugin_id: str, operation: str, *args: Any, **kwargs: Any):
        return get_plugin(plugin_id).cloud_operation(operation, *args, **kwargs)


printer_api = PrinterAPI()


class PrinterConnectionAdapter:
    """Route existing core method calls through an addon's declared handlers.

    A plugin connection only needs the lifecycle contract and shared state;
    optional printer functions may be implemented entirely in command_handlers.
    The bundled adapter keeps non-command attributes for compatibility with
    existing diagnostics, while undeclared methods cannot bypass the contract.
    """

    def __init__(self, config: PrinterConfig, connection: Any):
        self._config = config
        self._connection = connection
        self._plugin = get_plugin(config.plugin_id)
        self._event_bridge: HostEventBridge | None = None

    @property
    def state(self) -> Any:
        """Legacy providers retain their state; API2 exposes HostPrinterState.

        The adapter is the compatibility boundary for existing host consumers,
        whose historical provider-specific state types differ across drivers.
        Typed resource consumers use snapshot_device() instead.
        """
        from backend.app.printers.sdk.host import state_for_host

        snapshot = self._event_bridge.latest_snapshot if self._event_bridge is not None else None
        return state_for_host(self._config, self._plugin, snapshot or self._connection.state)

    @state.setter
    def state(self, state: PrinterState) -> None:
        self._connection.state = state

    @property
    def last_connect_error(self):
        return getattr(self._connection, "last_connect_error", None)

    @property
    def logging_enabled(self):
        return getattr(self._connection, "logging_enabled", False)

    @property
    def _drying_targets(self):
        return getattr(self._connection, "_drying_targets", {})

    def connect(self, loop: Any = None):
        if loop is None:
            return self._connection.connect()
        return self._connection.connect(loop=loop)

    def disconnect(self, timeout: float = 0):
        if timeout == 0:
            return self._connection.disconnect()
        return self._connection.disconnect(timeout=timeout)

    def check_staleness(self) -> bool:
        return self._connection.check_staleness()

    async def execute_standard(
        self, operation_id: str, parameters: dict | None = None, *, resource_id: str | None = None
    ):
        """Await an API2 operation; host authorization/preflight remains upstream."""
        from backend.app.printers.sdk import OperationRequest

        return await printer_api.invoke(
            self._config,
            self._connection,
            OperationRequest(operation_id=operation_id, resource_id=resource_id, parameters=parameters or {}),
        )

    async def execute_host_command(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Bridge existing async host workflows to a typed API2 job operation.

        API1 remains compatible. API2 tools/material paths use explicit stable
        IDs; this helper never infers a tool from a Bambu slot number.
        """
        import inspect

        if name in self._plugin.operations:
            result = self._plugin.execute(self._config, self._connection, name, kwargs, positional=args)
            return await result if inspect.isawaitable(result) else result
        operation = {
            "start_print": "job.start",
            "stop_print": "job.stop",
            "pause_print": "job.pause",
            "resume_print": "job.resume",
            "request_status_update": "telemetry.refresh",
        }.get(name)
        if self._plugin.api_version != 2 or operation is None:
            raise UnsupportedPrinterOperation(f"Plugin {self._plugin.id!r} has no standard host command {name!r}")
        if name == "start_print":
            from backend.app.printers.sdk.jobs import job_from_host_command, validate_job_request

            device = printer_api.describe_device(self._config)
            job = validate_job_request(device, job_from_host_command(device, *args, **kwargs))
            parameters = job.model_dump(mode="json", exclude_none=True)
        else:
            if args or kwargs:
                raise UnsupportedPrinterOperation("Standard job controls do not accept vendor-specific arguments")
            parameters = {}
        receipt = await self.execute_standard(operation, parameters)
        return receipt.status in {"accepted", "running", "succeeded"} and receipt.result is not False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return getattr(self._connection, name)
        if name in self._plugin.operations:

            def command(*args: Any, **kwargs: Any):
                return self._plugin.execute(
                    self._config,
                    self._connection,
                    name,
                    kwargs,
                    positional=args,
                )

            return command
        value = getattr(self._connection, name, None)
        if value is not None and not callable(value):
            return value
        if name in {"ip_address", "serial_number", "access_code", "model"}:
            return getattr(self._config, name)

        # Preserve getattr(..., default) and hasattr semantics for optional
        # metadata. Only actual methods and declared connection operations
        # turn into the controlled unsupported-operation response.
        if callable(value) or any(name in plugin.operations for plugin in list_plugins()):

            def unsupported(*args: Any, **kwargs: Any):
                raise UnsupportedPrinterOperation(f"Plugin {self._plugin.id!r} does not expose command {name!r}")

            return unsupported
        raise AttributeError(name)


def wrap_connection(printer: Any, connection: Any) -> PrinterConnectionAdapter:
    if isinstance(connection, PrinterConnectionAdapter):
        return connection
    return PrinterConnectionAdapter(PrinterConfig.from_printer(printer), connection)


async def execute_host_command(connection: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Awaitable host dispatch supporting both API2 adapters and legacy mocks."""
    import inspect

    if isinstance(connection, PrinterConnectionAdapter):
        return await connection.execute_host_command(name, *args, **kwargs)
    value = getattr(connection, name)(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value

"""Core printer services used by bundled and installed addons.

This is an in-process API. HTTP entry points apply the same authentication,
printer scopes and permissions as the rest of Bambuddy before calling it.
"""

from __future__ import annotations

import inspect
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from backend.app.printers.contracts import PrinterConfig, PrinterPlugin, PrinterPluginError, UnsupportedPrinterOperation
from backend.app.printers.registry import get_plugin, list_plugins, plugin_for

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
    return plugin_for(config) if config is not None else get_plugin(plugin_id or "bambu")


def _provider(config: PrinterConfig, connection: Any = None):
    return connection._plugin if isinstance(connection, PrinterConnectionAdapter) else plugin_for(config)


def operation_lease(printer_id: int | None, *, dispatch: bool = False):
    if printer_id is None:
        return nullcontext()
    from backend.app.printers.rolling import PrinterUpdateInProgress, operation_lease as provider_lease, rollouts

    if dispatch and rollouts.dispatch_blocked(printer_id):
        raise PrinterUpdateInProgress("Printer support is updating before its next job")
    return provider_lease(printer_id)


def leased_call(config: PrinterConfig, callback, *args, dispatch: bool = False, **kwargs):
    """Keep a version leased through both synchronous and asynchronous I/O."""
    from backend.app.printers.registry import provider_context, registry

    lease = operation_lease(config.printer_id, dispatch=dispatch)
    lease.__enter__()
    reference = None
    try:
        owner = getattr(callback, "__self__", None)
        if isinstance(owner, PrinterPlugin) and owner is not plugin_for(config):
            raise PrinterPluginError("Printer support changed; obtain its current connection before retrying")
        if isinstance(owner, PrinterPlugin):
            reference = "call:" + uuid4().hex
            registry.retain_runtime(owner, reference, config.printer_id, "request in progress")
        with (
            provider_context(config, owner)
            if isinstance(owner, PrinterPlugin) and config.printer_id is not None
            else nullcontext()
        ):
            result = callback(*args, **kwargs)
    except BaseException:
        if reference is not None:
            registry.release_runtime(reference)
        lease.__exit__(None, None, None)
        raise
    if inspect.isawaitable(result):

        async def finish():
            try:
                with (
                    provider_context(config, owner)
                    if isinstance(owner, PrinterPlugin) and config.printer_id is not None
                    else nullcontext()
                ):
                    return await result
            finally:
                if reference is not None:
                    registry.release_runtime(reference)
                lease.__exit__(None, None, None)

        return finish()
    if reference is not None:
        registry.release_runtime(reference)
    lease.__exit__(None, None, None)
    return result


class PrinterAPI:
    version = 2
    legacy_version = 1

    def describe_device(self, printer: Any):
        from backend.app.printers.sdk import validate_device

        config = PrinterConfig.from_printer(printer)
        return validate_device(plugin_for(config).describe_device(config))

    def snapshot_device(self, printer: Any, connection: Any):
        from backend.app.printers.sdk import validate_snapshot

        config = PrinterConfig.from_printer(printer)
        if isinstance(connection, PrinterConnectionAdapter) and connection._event_bridge is not None:
            snapshot = connection._event_bridge.latest_snapshot
            if snapshot is not None:
                return validate_snapshot(self.describe_device(config), snapshot)
        raw = connection._connection if isinstance(connection, PrinterConnectionAdapter) else connection
        return validate_snapshot(
            self.describe_device(config), _provider(config, connection).snapshot_device(config, raw)
        )

    async def invoke(self, printer: Any, connection: Any, request):
        import asyncio
        import inspect

        from backend.app.printers.registry import provider_context
        from backend.app.printers.sdk import OperationRequest, validate_invocation, validate_receipt

        config = PrinterConfig.from_printer(printer)
        request = request if isinstance(request, OperationRequest) else OperationRequest.model_validate(request)
        with operation_lease(config.printer_id, dispatch=request.operation_id == "job.start"):
            plugin = _provider(config, connection)
            if isinstance(connection, PrinterConnectionAdapter) and plugin is not plugin_for(config):
                raise PrinterPluginError("Printer support changed; obtain its current connection before retrying")
            descriptor = plugin.describe_device(config)
            operation = validate_invocation(descriptor, request)
            raw = connection._connection if isinstance(connection, PrinterConnectionAdapter) else connection

            async def perform():
                with provider_context(config, plugin):
                    if inspect.iscoroutinefunction(plugin.invoke):
                        return await plugin.invoke(config, raw, request)
                    result = await asyncio.to_thread(plugin.invoke, config, raw, request)
                    return await result if inspect.isawaitable(result) else result

            receipt = await asyncio.wait_for(perform(), timeout=operation.timeout_seconds)
            receipt = validate_receipt(operation, request, receipt)
            if isinstance(connection, PrinterConnectionAdapter):
                connection.track_operation(operation, request, receipt)
            return receipt

    def create_connection(self, printer: Any, *, provider: Any = None, **callbacks: Any):
        config = PrinterConfig.from_printer(printer)
        plugin = provider if provider is not None else plugin_for(config)
        plugin.validate_config(config)
        from backend.app.printers.sdk.host import adapt_callbacks

        adapted = adapt_callbacks(config, plugin, callbacks)
        connection = plugin.create_connection(config, **adapted)
        bind_printer(config)
        adapter = wrap_connection(config, connection, provider=plugin)
        adapter._event_bridge = adapted.event_bridge
        return adapter

    def capabilities(self, printer: Any) -> dict:
        config = PrinterConfig.from_printer(printer)
        plugin = plugin_for(config)
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
        return leased_call(
            config,
            _provider(config, connection).execute,
            config,
            raw,
            operation,
            args or {},
            dispatch=operation == "start_print",
        )

    def files(self, printer: Any, operation: str, *args: Any, **kwargs: Any):
        config = PrinterConfig.from_printer(printer)
        return leased_call(config, plugin_for(config).file_operation_for, config, operation, *args, **kwargs)

    def camera(self, printer: Any, operation: str, *args: Any, **kwargs: Any):
        config = PrinterConfig.from_printer(printer)
        return leased_call(config, plugin_for(config).camera_operation_for, config, operation, *args, **kwargs)

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
        return plugin_for(config).firmware_operation(operation, *args, **kwargs)

    def diagnostic(self, printer: Any, operation: str, *args: Any, **kwargs: Any):
        config = PrinterConfig.from_printer(printer)
        return plugin_for(config).diagnostic_operation(operation, *args, **kwargs)

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

    def __init__(self, config: PrinterConfig, connection: Any, *, provider: Any = None):
        self._config = config
        self._connection = connection
        self._plugin = provider if provider is not None else plugin_for(config)
        self._event_bridge: HostEventBridge | None = None
        self._active_tasks: dict[str, tuple[Any, Any]] = {}

    def track_operation(self, operation, request, receipt) -> None:
        if receipt.task_id and receipt.status in {"accepted", "running"}:
            from backend.app.printers.registry import registry

            self._active_tasks[receipt.task_id] = (operation, request)
            registry.retain_runtime(
                self._plugin,
                f"adapter-task:{id(self)}:{receipt.task_id}",
                self._config.printer_id,
                "pending operation",
            )

    async def poll_pending_operations(self) -> bool:
        """Observe accepted work on its original provider; never resubmit it."""
        import asyncio

        from backend.app.printers.registry import registry
        from backend.app.printers.sdk import validate_receipt

        for task_id, (operation, request) in tuple(self._active_tasks.items()):
            try:
                result = await asyncio.wait_for(
                    self._plugin.operation_status(self._config, self._connection, task_id),
                    timeout=min(operation.timeout_seconds, 5),
                )
                receipt = validate_receipt(operation, request, result)
            except Exception:
                continue
            if receipt.status in {"succeeded", "failed", "cancelled"}:
                self._active_tasks.pop(task_id, None)
                registry.release_runtime(f"adapter-task:{id(self)}:{task_id}")
        return not self._active_tasks

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
            self,
            OperationRequest(operation_id=operation_id, resource_id=resource_id, parameters=parameters or {}),
        )

    async def execute_host_command(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Bridge existing async host workflows to a typed API2 job operation.

        API1 remains compatible. API2 tools/material paths use explicit stable
        IDs; this helper never infers a tool from a Bambu slot number.
        """
        import inspect

        if name in self._plugin.operations:
            result = leased_call(
                self._config,
                self._plugin.execute,
                self._config,
                self._connection,
                name,
                kwargs,
                positional=args,
                dispatch=name == "start_print",
            )
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
                return leased_call(
                    self._config,
                    self._plugin.execute,
                    self._config,
                    self._connection,
                    name,
                    kwargs,
                    positional=args,
                    dispatch=name == "start_print",
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


def wrap_connection(printer: Any, connection: Any, *, provider: Any = None) -> PrinterConnectionAdapter:
    if isinstance(connection, PrinterConnectionAdapter):
        return connection
    return PrinterConnectionAdapter(PrinterConfig.from_printer(printer), connection, provider=provider)


async def execute_host_command(connection: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Awaitable host dispatch supporting both API2 adapters and legacy mocks."""
    import inspect

    if isinstance(connection, PrinterConnectionAdapter):
        return await connection.execute_host_command(name, *args, **kwargs)
    value = getattr(connection, name)(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value

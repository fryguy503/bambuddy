"""Versioned in-process printer addon contract.

Plugins are trusted installed Python packages, discovered through the
``bambuddy.printers`` entry point group. They translate protocol details into
shared state, events and command results; core owns persistence and workflows.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

PRINTER_API_VERSION = 1
SUPPORTED_PRINTER_API_VERSIONS = (1, 2)

if TYPE_CHECKING:
    from backend.app.printers.sdk import (
        DeviceDescriptor,
        DeviceSnapshot,
        OperationDescriptor,
        OperationReceipt,
        OperationRequest,
        PluginManifest,
    )
    from backend.app.printers.state import PrinterState


class PrinterPluginError(ValueError):
    """A configured printer plugin cannot satisfy the request."""


class UnknownPrinterPlugin(PrinterPluginError):
    pass


class UnsupportedPrinterOperation(PrinterPluginError):
    pass


@dataclass(frozen=True)
class PrinterConfig:
    ip_address: str
    serial_number: str
    access_code: str = field(repr=False)
    model: str | None = None
    plugin_id: str = "bambu"
    printer_id: int | None = None
    options: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_printer(cls, printer: Any) -> PrinterConfig:
        if isinstance(printer, cls):
            return printer
        return cls(
            ip_address=printer.ip_address,
            serial_number=printer.serial_number,
            access_code=printer.access_code,
            model=printer.model,
            plugin_id=getattr(printer, "plugin_id", None) or "bambu",
            printer_id=getattr(printer, "id", None),
            options=getattr(printer, "plugin_options", None) or {},
        )


class PrinterConnection(Protocol):
    """Minimum lifecycle every printer implements; optional commands are declared."""

    @property
    def state(self) -> PrinterState | DeviceSnapshot: ...

    def connect(self, loop: Any = None) -> Any: ...
    def disconnect(self, timeout: float = 0) -> Any: ...
    def check_staleness(self) -> bool: ...


@dataclass(frozen=True)
class PrinterModel:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    image_url: str | None = None
    gcode_compatibility_group: str | None = None
    device_template: DeviceDescriptor | None = None

    def to_dict(self) -> dict:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "aliases": list(self.aliases),
            "capabilities": sorted(self.capabilities),
            "image_url": self.image_url,
            "gcode_compatibility_group": self.gcode_compatibility_group,
        }
        if self.device_template is not None:
            result["device_template"] = self.device_template.model_dump(mode="json")
        return result


class PrinterPlugin(ABC):
    """Implement this class and publish a ``bambuddy.printers`` entry point.

    Optional backends use explicit allowlists in each adapter. An absent
    operation raises UnsupportedPrinterOperation; core never guesses a vendor
    or treats an arbitrary attribute as a callable command.
    """

    id: str
    name: str
    version: str = "1.0.0"
    api_version: Literal[1, 2] = 1
    bundled: bool = False
    models: tuple[PrinterModel, ...] = ()
    capabilities: frozenset[str] = frozenset()
    operations: tuple[str, ...] = ()
    operation_descriptors: tuple[OperationDescriptor, ...] = ()
    configuration_schema: dict = {"type": "object"}
    manifest_data: Any = None
    publisher_id: str = ""
    _installed_manifest: PluginManifest | None = None
    _package_namespace: str | None = None

    def sdk_manifest(self) -> PluginManifest:
        """Code metadata is checked against the package's static manifest on load."""
        from backend.app.printers.sdk import PluginManifest, validate_manifest

        installed = getattr(self, "_installed_manifest", None)
        if installed is not None:
            return validate_manifest(installed)
        if self.manifest_data is not None:
            return validate_manifest(self.manifest_data)
        return PluginManifest(
            plugin_id=self.id,
            name=self.name,
            version=self.version,
            api_version=self.api_version,
            requires_api=">=1.0,<2.0" if self.api_version == 1 else ">=2.0,<3.0",
            entrypoint=f"{type(self).__module__}:{type(self).__name__}",
            publisher_id=self.publisher_id or self.id,
            configuration_schema=self.configuration_schema,
            operations=list(self.operation_descriptors),
            models=[model.to_dict() for model in self.models],
            capabilities=sorted(self.capabilities),
        )

    def connection_fields(self) -> dict[str, dict]:
        """Optional standard fields; other transports may use only options."""
        return {}

    def effective_configuration_schema(self) -> dict:
        installed = getattr(self, "_installed_manifest", None)
        return installed.configuration_schema if installed is not None else self.configuration_schema

    def describe_device(self, config: PrinterConfig) -> DeviceDescriptor:
        """Describe arbitrary resources; API1 has no automatic vendor guesses."""
        model = self.model(config.model)
        if model is None or model.device_template is None:
            raise UnsupportedPrinterOperation(f"Plugin {self.id!r} has no resource descriptor for this device")
        return model.device_template.model_copy(
            update={
                "device_id": config.serial_number or str(config.printer_id),
                "configuration_schema": self.effective_configuration_schema(),
            }
        )

    def snapshot_device(self, config: PrinterConfig, connection: PrinterConnection) -> DeviceSnapshot:
        """Return normalized API2 telemetry, never legacy raw protocol data."""
        from backend.app.printers.sdk import DeviceSnapshot

        state = connection.state
        snapshot = state if isinstance(state, DeviceSnapshot) else getattr(state, "device_snapshot", None)
        if snapshot is None:
            raise UnsupportedPrinterOperation(f"Plugin {self.id!r} has not supplied a device snapshot")
        from backend.app.printers.sdk import validate_snapshot

        return validate_snapshot(self.describe_device(config), snapshot)

    def operation_handlers(self, connection: PrinterConnection) -> Mapping[str, Callable]:
        """API2 handlers receive one OperationRequest containing resource identity."""
        return {}

    async def invoke(
        self, config: PrinterConfig, connection: PrinterConnection, request: OperationRequest
    ) -> OperationReceipt:
        import asyncio
        import inspect

        from backend.app.printers.sdk import OperationReceipt, validate_invocation, validate_receipt

        self.validate_config(config)
        descriptor = self.describe_device(config)
        operation = validate_invocation(descriptor, request)
        handler = self.operation_handlers(connection).get(request.operation_id)
        if handler is None:
            raise UnsupportedPrinterOperation(
                f"Plugin {self.id!r} does not implement operation {request.operation_id!r}"
            )
        inspect.signature(handler).bind(request)
        if inspect.iscoroutinefunction(handler):
            value = await handler(request)
        else:
            value = await asyncio.to_thread(handler, request)
            if inspect.isawaitable(value):
                value = await value
        if not isinstance(value, OperationReceipt):
            value = OperationReceipt(
                operation_id=request.operation_id, request_id=request.request_id, status="succeeded", result=value
            )
        return validate_receipt(operation, request, value)

    async def operation_status(
        self, config: PrinterConfig, connection: PrinterConnection, task_id: str
    ) -> OperationReceipt:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} has no asynchronous task status service")

    async def cancel_operation(
        self, config: PrinterConfig, connection: PrinterConnection, task_id: str
    ) -> OperationReceipt:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not support cancelling tasks")

    def can_deactivate(self, config: PrinterConfig, connection: PrinterConnection) -> bool:
        """False while a provider-specific operation makes replacement unsafe."""
        return True

    def descriptor(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "bundled": self.bundled,
            "capabilities": sorted(self.capabilities),
            "operations": list(self.operations),
            "models": [model.to_dict() for model in self.models],
            "configuration_schema": self.effective_configuration_schema(),
            "connection_fields": self.connection_fields(),
        }

    def normalize_serial_number(self, value: str) -> str:
        return value.strip()

    async def recover_connection(
        self, config: PrinterConfig, connection: PrinterConnection, offline_grace: float
    ) -> bool | None:
        """Optional recovery: None skips, False probed, True attempted a rebuild."""
        return None

    def validate_config(self, config: PrinterConfig) -> None:
        if config.plugin_id != self.id:
            raise PrinterPluginError("Printer configuration belongs to a different plugin")
        from backend.app.printers.sdk import validate_value

        validate_value(dict(config.options), self.effective_configuration_schema(), path="plugin_options")

    def model(self, model_id: str | None) -> PrinterModel | None:
        key = (model_id or "").strip().casefold().replace(" ", "").replace("-", "")
        for model in self.models:
            if any(key == name.casefold().replace(" ", "").replace("-", "") for name in (model.id, *model.aliases)):
                return model
        return None

    def model_capabilities(self, model_id: str | None) -> frozenset[str]:
        model = self.model(model_id)
        return self.capabilities if model is None else model.capabilities

    def model_operation(self, operation: str, *args: Any) -> Any:
        """Model compatibility policy belongs to the selected driver package."""
        if operation in {"normalize_printer_model", "normalize_printer_model_id"}:
            value = args[0]
            model = self.model(value)
            return model.id if model else value.strip() if value else None
        if operation == "is_gcode_compatible":
            source, target = args
            if not source or not target:
                return True
            first, second = self.model(source), self.model(target)
            if first is None or second is None:
                return source.strip().casefold() == target.strip().casefold()
            return first.id == second.id or bool(
                first.gcode_compatibility_group and first.gcode_compatibility_group == second.gcode_compatibility_group
            )
        mapping = {
            "uses_exhaust_fan_label": "exhaust_fan",
            "has_ethernet": "ethernet",
            "has_external_storage": "external_storage",
            "has_remote_storage_toggle": "remote_storage_toggle",
            "is_dual_nozzle_model": "dual_nozzle",
            "is_nozzle_rack_model": "nozzle_rack",
            "supports_nozzle_flow_type": "nozzle_flow_type",
            "uses_legacy_rfid_refresh": "legacy_rfid_refresh",
        }
        if operation in mapping:
            return mapping[operation] in self.model_capabilities(args[0])
        if operation == "aux_fan_sides":
            return "primary", "secondary"
        if operation == "get_rod_type":
            capabilities = self.model_capabilities(args[0])
            return next(
                (kind for kind in ("carbon", "steel_rod", "linear_rail") if f"rods_{kind}" in capabilities), None
            )
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} has no model policy {operation!r}")

    def feature_operation(self, operation: str, model: str | None, **options: Any) -> Any:
        mapping = {
            "supports_chamber_temp": "chamber_temperature",
            "supports_chamber_heater": "chamber_heater",
            "supports_airduct": "airduct",
            "has_stg_cur_idle_bug": "stage_idle_workaround",
            "drying_screen_only": "drying_screen_only",
            "supports_drying": "drying",
            "supports_drying_while_printing": "drying_while_printing",
        }
        capabilities = self.model_capabilities(model)
        if operation in mapping:
            return mapping[operation] in capabilities
        if operation == "display_temperatures":
            result = {}
            for key, value in (options.get("temperatures") or {}).items():
                if key.startswith("chamber") and "chamber_temperature" not in capabilities:
                    continue
                try:
                    result[key] = float(value)
                except (TypeError, ValueError):
                    continue
            return result
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} has no feature policy {operation!r}")

    def compatibility_module(self, name: str) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} has no legacy module {name!r}")

    def legacy_http_routers(self) -> tuple[Any, ...]:
        """Optional compatibility endpoints packaged and versioned by the provider."""
        return ()

    async def start_host_services(self, host: Any) -> None:
        """Start optional provider-owned background work after core startup."""
        return None

    async def stop_host_services(self, host: Any) -> None:
        """Stop provider-owned background work before core shutdown."""
        return None

    @abstractmethod
    def create_connection(self, config: PrinterConfig, **callbacks: Callable) -> PrinterConnection:
        raise NotImplementedError

    def create_file_client(self, config: PrinterConfig, **options: Any) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} has no file client")

    def file_operation(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement file operation {operation!r}")

    def file_operation_for(self, config: PrinterConfig, operation: str, *args: Any, **kwargs: Any) -> Any:
        """File operation with persisted options, including for offline printers.

        Existing adapters may keep file_operation(); adapters needing settings
        beyond the legacy transport arguments override this hook instead.
        """
        self.validate_config(config)
        return self.file_operation(operation, *args, **kwargs)

    def camera_operation(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement camera operation {operation!r}")

    def camera_operation_for(self, config: PrinterConfig, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Camera operation with persisted options independent of a connection."""
        self.validate_config(config)
        return self.camera_operation(operation, *args, **kwargs)

    def storage_operation(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        from backend.app.printers.storage import generic_storage_operation

        return generic_storage_operation(operation, *args, **kwargs)

    def storage_operation_for(self, config: PrinterConfig, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Storage rules with persisted options for the selected printer."""
        self.validate_config(config)
        return self.storage_operation(operation, *args, **kwargs)

    def discovery_operation(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement discovery operation {operation!r}")

    def diagnostic_operation(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement diagnostic operation {operation!r}")

    def firmware_operation(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement firmware operation {operation!r}")

    def cloud_operation(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement cloud operation {operation!r}")

    def virtual_printer_service(self) -> Any:
        """Optional slicer-facing virtual printer emulation supplied by a plugin."""
        raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement virtual printer emulation")

    def derive_status(self, state: PrinterState, model: str | None = None) -> str | None:
        return state.status_name

    def status_fingerprint(self, state) -> Any:
        """Optional protocol-only changes that should trigger a host broadcast."""
        return None

    def archive_filament_metadata(self, data: dict, mapping=None) -> dict[str, str]:
        """Normalized archive fields from a print event when its artifact is unavailable."""
        return {name: data[name] for name in ("filament_type", "filament_color") if isinstance(data.get(name), str)}

    def derive_failure_reason(self, status: str, data: dict) -> str | None:
        if status in {"cancelled", "aborted"}:
            return "userCancelled"
        reason = data.get("failure_reason")
        return reason if status == "failed" and isinstance(reason, str) else None

    async def cleanup_after_print(self, printer_id: int, data: dict, archive_id: int | None, logger) -> None:
        """Optional provider cleanup; core never guesses vendor artifact paths."""
        return None

    def loaded_materials(self, state: PrinterState) -> list[dict]:
        """Typed material sources retaining stable source and tool resource IDs."""
        if self.api_version == 1:
            # API1 explicitly used the historical shared filament-unit schema.
            # Keep its deprecated projection in the replaceable compatibility
            # package; API2 never fabricates numeric vendor tray identities.
            from backend.app.printers.compat import compatibility_module

            return compatibility_module("bambu", "materials").loaded_materials(state, self.normalize_telemetry(state))
        return list(getattr(state, "material_inventory", []) or [])

    def normalize_telemetry(self, state: PrinterState) -> dict:
        """Common filament state for workflows that consume a status snapshot."""
        return {
            "ams": state.filament_units,
            "vt_tray": state.external_spools,
            "ams_extruder_map": state.ams_extruder_map,
            "mapping": state.print_ams_mapping or [],
        }

    def serialize_state(
        self,
        state: PrinterState,
        printer_id: int | None = None,
        model: str | None = None,
        drying_targets: dict | None = None,
    ) -> dict:
        from backend.app.printers.serialization import common_state_to_dict

        return common_state_to_dict(state, self, printer_id=printer_id, model=model)

    def command_handlers(self, connection: PrinterConnection) -> Mapping[str, Callable]:
        return {}

    def execute(
        self,
        config: PrinterConfig,
        connection: PrinterConnection,
        operation: str,
        args: dict,
        *,
        positional: tuple = (),
    ) -> Any:
        self.validate_config(config)
        if operation not in self.operations:
            raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not expose command {operation!r}")
        handler = self.command_handlers(connection).get(operation)
        if handler is None:
            raise UnsupportedPrinterOperation(f"Plugin {self.id!r} does not implement command {operation!r}")
        # Validate before calling, so a TypeError inside a provider is not
        # mistaken for invalid client input after partially executing a command.
        from inspect import signature

        signature(handler).bind(*positional, **args)
        return handler(*positional, **args)

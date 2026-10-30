"""Anycubic LAN support as a replaceable API2 printer package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.printers.contracts import (
    PrinterConnection,
    PrinterModel,
    PrinterPlugin,
    PrinterPluginError,
    UnsupportedPrinterOperation,
)
from backend.app.printers.sdk import DeviceSnapshot, PluginManifest

from .models import MODELS, get_model
from .protocol import fingerprint, private_host

if TYPE_CHECKING:
    from .connection import AnycubicConnection

_CAPABILITIES = frozenset(
    {
        "print",
        "pause",
        "resume",
        "stop",
        "files",
        "camera",
        "discovery",
        "material",
        "material_drying",
        "manual_motion",
        "cloud_profiles",
    }
)


class AnycubicPrinterPlugin(PrinterPlugin):
    id = "anycubic"
    name = "Anycubic signed LAN"
    version = "0.1.1"
    api_version = 2
    publisher_id = "fryguy503"
    capabilities = _CAPABILITIES
    operations = (
        "request_status_update",
        "mark_power_off",
        "force_reconnect_stale_session",
        "enable_logging",
        "get_logs",
        "clear_logs",
        "pause_print",
        "resume_print",
        "stop_print",
        "set_chamber_light",
    )
    models = tuple(
        PrinterModel(
            model.name,
            model.name,
            aliases=(model.model_id, "Anycubic " + model.name),
            capabilities=_CAPABILITIES,
            image_url="/api/v1/printer-plugins/anycubic/assets/printers/anycubic-"
            + model.name.lower().replace(" ", "-")
            + ".png",
        )
        for model in MODELS.values()
    )
    configuration_schema = {
        "type": "object",
        "properties": {
            "tls_fingerprint": {
                "type": "string",
                "pattern": "^[0-9A-Fa-f]{64}$",
                "title": "Approved printer TLS fingerprint",
                "description": "Review the SHA-256 fingerprint from LAN identity inspection before saving. It pins this printer's certificate.",
            }
        },
        "required": ["tls_fingerprint"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.connections: dict[str, AnycubicConnection] = {}

    def sdk_manifest(self) -> PluginManifest:
        manifest = super().sdk_manifest()
        if self._installed_manifest is not None:
            return manifest
        # Anonymous printer-origin snapshot jobs require SDK 2.1. Existing
        # 2.0 hosts must reject this version from static metadata before import.
        return manifest.model_copy(update={"requires_api": ">=2.1.0,<3.0.0"})

    def connection_fields(self):
        return {
            "ip_address": {"label": "LAN IP address", "required": True},
            "serial_number": {"label": "Discovered printer identity", "required": True},
            "access_code": {"required": False},
        }

    def validate_config(self, config):
        super().validate_config(config)
        private_host(config.ip_address)
        approved_fingerprint = config.options.get("tls_fingerprint")
        if not isinstance(approved_fingerprint, str):
            raise PrinterPluginError("Review and approve the printer's TLS fingerprint")
        fingerprint(approved_fingerprint)
        if get_model(config.model) is None:
            raise PrinterPluginError("Select a supported signed-protocol Anycubic model")

    def model(self, model_id):
        selected = get_model(model_id)
        return super().model(selected.name if selected is not None else model_id)

    def create_connection(self, config, *, on_event=None, **callbacks):
        from dataclasses import replace

        from .connection import AnycubicConnection

        self.validate_config(config)
        selected = get_model(config.model)
        if selected is None:
            raise PrinterPluginError("Select a supported signed-protocol Anycubic model")
        config = replace(config, model=selected.name)
        connection = AnycubicConnection(config, self, on_event=on_event, **callbacks)
        self.connections[config.serial_number] = connection
        return connection

    def describe_device(self, config):
        from .device import describe

        connection = self.connections.get(config.serial_number)
        native = connection.native if connection is not None else None
        if native is not None:
            with native._lock:
                return describe(self, config, native)
        return describe(self, config)

    @staticmethod
    def _typed_connection(connection: PrinterConnection) -> AnycubicConnection:
        from .connection import AnycubicConnection

        if not isinstance(connection, AnycubicConnection):
            raise PrinterPluginError("The connection belongs to a different printer provider")
        return connection

    def snapshot_device(self, config, connection: PrinterConnection) -> DeviceSnapshot:
        return self._typed_connection(connection).state

    def operation_handlers(self, connection):
        native_connection = self._typed_connection(connection)
        return {
            operation.id: native_connection.invoke
            for operation in self.describe_device(native_connection.config).operations
        }

    def command_handlers(self, connection):
        native_connection = self._typed_connection(connection)
        return {name: getattr(native_connection.native, name) for name in self.operations}

    async def operation_status(self, config, connection, task_id):
        return await self._typed_connection(connection).operation_status(task_id)

    async def cancel_operation(self, config, connection, task_id):
        return await self._typed_connection(connection).cancel_operation(task_id)

    def can_deactivate(self, config, connection):
        connection = getattr(connection, "_connection", connection)
        return self._typed_connection(connection).can_deactivate()

    def _connection(self, config):
        connection = self.connections.get(config.serial_number)
        if connection is None:
            raise PrinterPluginError("Connect the Anycubic printer before using its native transport")
        return connection

    def file_operation_for(self, config, operation, *args, **kwargs):
        from .transport import file_operation

        self.validate_config(config)
        if operation == "ftps_handshake_blocked":
            return False
        return file_operation(self._connection(config), operation, *args, **kwargs)

    def camera_operation_for(self, config, operation, *args, **kwargs):
        from .transport import camera_operation

        self.validate_config(config)
        return camera_operation(self._connection(config), operation, *args, **kwargs)

    def discovery_operation(self, operation, *args, **kwargs):
        from .discovery import discovery_service, subnet_scanner

        if operation == "service":
            return discovery_service
        if operation == "subnet_scanner":
            return subnet_scanner
        if operation == "inspect":
            from .client import AnycubicMQTTClient

            return AnycubicMQTTClient.discover(*args, **kwargs)
        raise UnsupportedPrinterOperation(operation)

    def legacy_http_routers(self):
        from .cloud_routes import router as cloud_router
        from .routes import router

        return (router, cloud_router)

    def cloud_operation(self, operation, *args, **kwargs):
        from .cloud import cloud_operation

        return cloud_operation(operation, *args, **kwargs)

    async def cleanup_after_print(self, printer_id, data, archive_id, logger):
        from backend.app.services.printer_manager import printer_manager

        printer_manager.set_awaiting_plate_clear(printer_id, True)
        connection = next((item for item in self.connections.values() if item.config.printer_id == printer_id), None)
        if connection is not None:
            job_id = data.get("job_id") or data.get("subtask_id")
            record = connection.journal.get(job_id) if job_id else None
            if record is not None and record.get("pending_outcome"):
                from . import inventory

                outcome = record["pending_outcome"]
                try:
                    result = await inventory.complete(connection.config, {**record, "archive_id": archive_id}, outcome)
                    deferred = isinstance(result, dict) and result.get("state") == "deferred"
                except Exception:
                    logger.warning("Anycubic usage accounting deferred after verified print outcome", exc_info=True)
                    deferred = True
                connection.journal.update(job_id, status=outcome, pending_outcome=outcome if deferred else None)

    async def start_host_services(self, host):
        # Cloud reads are on demand; installing the plugin starts no account
        # polling or automatic sign-in in the background.
        return None

    async def stop_host_services(self, host):
        from .discovery import anycubic_scanner

        await anycubic_scanner.stop()
        for connection in list(self.connections.values()):
            connection.disconnect()

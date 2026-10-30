"""Bundled Bambu Lab adapter. All Bambu protocol choices stay in this package."""

from __future__ import annotations

from typing import Any

from backend.app.printers.contracts import (
    PrinterConfig,
    PrinterModel,
    PrinterPlugin,
    UnsupportedPrinterOperation,
)

_BASE = frozenset(
    {"print", "pause", "resume", "stop", "gcode", "files", "camera", "discovery", "firmware", "cloud", "ams"}
)
_IMAGES = {
    "X1": "x1c",
    "X1C": "x1c",
    "X1E": "x1e",
    "X2D": "x2d",
    "P1P": "p1p",
    "P1S": "p1s",
    "P2S": "p2s",
    "A1": "a1",
    "A1 Mini": "a1mini",
    "A2L": "a2l",
    "H2D": "h2d",
    "H2D Pro": "h2dpro",
    "H2C": "h2c",
    "H2S": "h2d",
}
_ALIASES = {
    "X1": ("BL-P002", "Bambu Lab X1"),
    "X1C": ("BL-P001", "X1 Carbon", "Bambu Lab X1 Carbon"),
    "X1E": ("C13", "BL-P003", "Bambu Lab X1E"),
    "X2D": ("N6", "Bambu Lab X2D"),
    "P1P": ("C11", "Bambu Lab P1P"),
    "P1S": ("C12", "Bambu Lab P1S"),
    "P2S": ("N7", "Bambu Lab P2S"),
    "A1": ("N2S", "A11", "Bambu Lab A1"),
    "A1 Mini": ("N1", "A04", "A12", "A1M", "Bambu Lab A1 Mini", "Bambu Lab A1M"),
    "A2L": ("N9", "Bambu Lab A2L"),
    "H2D": ("O1D", "Bambu Lab H2D"),
    "H2D Pro": ("O1E", "O2D", "Bambu Lab H2D Pro"),
    "H2C": ("O1C", "O1C2", "Bambu Lab H2C"),
    "H2S": ("O1S", "Bambu Lab H2S"),
}


def _models() -> tuple[PrinterModel, ...]:
    from .capabilities import supports_airduct, supports_chamber_heater, supports_chamber_temp

    models = []
    for model_id, aliases in _ALIASES.items():
        capabilities = set(_BASE)
        if supports_chamber_temp(model_id):
            capabilities.add("chamber_temperature")
        if supports_chamber_heater(model_id):
            capabilities.add("chamber_heater")
        if supports_airduct(model_id):
            capabilities.add("airduct")
        if model_id in {"A1", "A1 Mini", "A2L"}:
            capabilities.add("bed_slinger")
        if model_id not in {"A1", "A1 Mini", "A2L", "P1P"}:
            capabilities.add("chamber_fan")
            if model_id != "P1S":
                capabilities.add("door_sensor")
        if model_id in {"P2S", "X2D"}:
            capabilities.add("exhaust_fan")
        if model_id == "X2D":
            capabilities.add("aux_fan_left")
        if model_id in {"H2D", "H2D Pro", "H2C", "X2D"}:
            capabilities.add("dual_nozzle")
        from . import models as model_rules

        for method, capability in {
            "has_ethernet": "ethernet",
            "has_external_storage": "external_storage",
            "has_remote_storage_toggle": "remote_storage_toggle",
            "is_nozzle_rack_model": "nozzle_rack",
            "supports_nozzle_flow_type": "nozzle_flow_type",
            "uses_legacy_rfid_refresh": "legacy_rfid_refresh",
        }.items():
            if getattr(model_rules, method)(model_id):
                capabilities.add(capability)
        rod_type = model_rules.get_rod_type(model_id)
        if rod_type:
            capabilities.add(f"rods_{rod_type}")
        models.append(
            PrinterModel(
                id=model_id,
                name="X1 Carbon" if model_id == "X1C" else model_id,
                aliases=aliases,
                capabilities=frozenset(capabilities),
                image_url=f"/api/v1/printer-plugins/bambu/assets/printers/{_IMAGES[model_id]}.png",
                gcode_compatibility_group="bambu-x1-p1" if model_id in {"X1", "X1C", "X1E", "P1P", "P1S"} else None,
            )
        )
    return tuple(models)


# Exact protocol entry points exposed to the internal addon command API.
_COMMANDS = (
    "mark_power_off",
    "force_reconnect_stale_session",
    "register_assignment_verification",
    "register_raw_message_handler",
    "unregister_raw_message_handler",
    "publish_raw",
    "await_cali_ack",
    "start_print",
    "stop_print",
    "pause_print",
    "resume_print",
    "request_status_update",
    "send_gcode",
    "set_bed_temperature",
    "set_nozzle_temperature",
    "set_chamber_temperature",
    "set_print_speed",
    "set_fan_speed",
    "set_airduct_mode",
    "set_chamber_light",
    "select_extruder",
    "set_xcam_option",
    "set_ams_filament_backup",
    "start_calibration",
    "clear_hms_errors",
    "skip_objects",
    "send_drying_command",
    "get_kprofiles",
    "set_kprofile",
    "set_kprofiles_batch",
    "delete_kprofile",
    "ams_load_filament",
    "ams_unload_filament",
    "ams_control",
    "ams_refresh_tray",
    "ams_set_filament_setting",
    "reset_ams_slot",
    "extrusion_cali_sel",
    "extrusion_cali_set",
    "execute_hms_action",
    "enable_logging",
    "get_logs",
    "clear_logs",
)
_FILE_OPERATIONS = frozenset(
    {
        "download_file_async",
        "download_file_try_paths_async",
        "upload_file_async",
        "list_files_async",
        "list_files_result_async",
        "find_remote_file_async",
        "delete_file_async",
        "download_file_bytes_async",
        "remote_file_settled",
        "delete_archived_timelapse",
        "get_storage_info_async",
        "ftps_handshake_blocked",
        "normalize_3mf_name",
        "cache_3mf_download",
        "get_cached_3mf",
        "clear_3mf_cache",
        "describe_upload_failure",
        "get_ftp_retry_settings",
        "with_ftp_retry",
    }
)
_CAMERA_OPERATIONS = frozenset(
    {
        "supports_rtsp",
        "get_camera_port",
        "is_chamber_image_model",
        "read_chamber_image_frame",
        "generate_chamber_image_stream",
        "read_next_chamber_frame",
        "capture_camera_frame",
        "capture_in_flight",
        "capture_camera_frame_bytes",
        "capture_finish_photo",
        "test_camera_connection",
        "get_camera_profile",
    }
)


class BambuPrinterPlugin(PrinterPlugin):
    id = "bambu"
    name = "Bambu Lab"
    version = "1.0.0"
    api_version = 2
    publisher_id = "bambuddy"
    bundled = True
    capabilities = _BASE
    models = _models()
    operations = _COMMANDS

    def sdk_manifest(self):
        manifest = super().sdk_manifest()
        if self._installed_manifest is not None:
            return manifest
        return manifest.model_copy(
            update={
                "requires_core": ">=1.3.1b1,<2.0",
                "dependencies": {"paho-mqtt": ">=2.1,<3.0"},
            }
        )

    def connection_fields(self):
        from .device import connection_fields

        return connection_fields()

    def legacy_http_routers(self):
        from .cloud_routes import router as cloud_router
        from .kprofile_routes import router as kprofile_router
        from .legacy_routes import router

        return (router, cloud_router, kprofile_router)

    async def start_host_services(self, host) -> None:
        host.start_ams_history_recording()

    async def stop_host_services(self, host) -> None:
        host.stop_ams_history_recording()

    def validate_config(self, config: PrinterConfig) -> None:
        import re

        super().validate_config(config)
        if (
            not isinstance(config.serial_number, str)
            or not config.serial_number.strip()
            or len(config.serial_number) > 50
        ):
            raise ValueError("serial_number must contain 1 to 50 characters")
        if not isinstance(config.access_code, str) or not 1 <= len(config.access_code) <= 20:
            raise ValueError("access_code must contain 1 to 20 characters")
        host_pattern = r"^(\d{1,3}(\.\d{1,3}){3}|[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*)$"
        if (
            not isinstance(config.ip_address, str)
            or len(config.ip_address) > 253
            or not re.fullmatch(host_pattern, config.ip_address)
        ):
            raise ValueError("ip_address must be an IP address or hostname")

    def describe_device(self, config):
        import sys

        from .device import describe

        manager_module = sys.modules.get("backend.app.services.printer_manager")
        manager = getattr(manager_module, "printer_manager", None)
        client = (
            manager.get_client(config.printer_id) if manager is not None and config.printer_id is not None else None
        )
        state = client.state if client is not None else None

        return describe(config, self.version, state)

    def snapshot_device(self, config, connection):
        from .device import snapshot

        return snapshot(config, connection, self.describe_device(config))

    def operation_handlers(self, connection):
        from .device import handlers

        return handlers(connection)

    async def invoke(self, config, connection, request):
        from backend.app.printers.sdk import OperationError

        receipt = await super().invoke(config, connection, request)
        if receipt.status == "succeeded" and receipt.result is False:
            return receipt.model_copy(
                update={
                    "status": "failed",
                    "error": OperationError(
                        code="printer_command_rejected", message="The printer did not accept the command"
                    ),
                }
            )
        return receipt

    def can_deactivate(self, config, connection) -> bool:
        from typing import cast

        from .legacy_state import PrinterState

        state = cast(PrinterState, connection.state)
        if not state.connected:
            return True
        if state.state in {"RUNNING", "PREPARE", "PAUSE", "SLICING"}:
            return False
        if getattr(state, "ams_status", 0) or getattr(getattr(state, "fila_switch", None), "stat", 0):
            return False
        raw = state.raw_data if isinstance(state.raw_data, dict) else {}
        for unit in raw.get("ams", []) if isinstance(raw.get("ams", []), list) else []:
            if isinstance(unit, dict) and (unit.get("dry_time", 0) or 0) > 0:
                return False
        from .mqtt import get_stage_name

        stage = get_stage_name(state.stg_cur) or ""
        return "calibrat" not in stage.casefold()

    def execute(self, config, connection, operation, args, *, positional=()):
        # Legacy core callers still use method-shaped commands. Hardware
        # limits are enforced here inside the plugin, alongside the typed
        # resource API, rather than by global application validation ceilings.
        limits = {"set_nozzle_temperature": 320, "set_bed_temperature": 140, "set_chamber_temperature": 65}
        if operation in limits:
            import math

            target = args.get("target", positional[0] if positional else None)
            if (
                not isinstance(target, (int, float))
                or isinstance(target, bool)
                or not math.isfinite(target)
                or not 0 <= target <= limits[operation]
            ):
                raise ValueError(
                    f"Target temperature must be within this heater's supported range: 0..{limits[operation]}"
                )
        return super().execute(config, connection, operation, args, positional=positional)

    def normalize_serial_number(self, value: str) -> str:
        return value.strip().upper()

    def compatibility_module(self, name: str):
        from importlib import import_module

        if name not in {"models", "capabilities", "serializer", "legacy_state"}:
            raise UnsupportedPrinterOperation(f"Unsupported Bambu legacy module {name!r}")
        return import_module(f".{name}", __package__)

    def model_operation(self, operation: str, *args):
        module = self.compatibility_module("models")
        if operation == "constant":
            name = args[0]
            if not isinstance(name, str) or not name.isupper() or name.startswith("_"):
                raise UnsupportedPrinterOperation("Unsupported Bambu model constant")
            return getattr(module, name)
        if operation.startswith("_") or not callable(getattr(module, operation, None)):
            raise UnsupportedPrinterOperation(f"Unsupported Bambu model policy {operation!r}")
        return getattr(module, operation)(*args)

    def feature_operation(self, operation: str, model: str | None, **options):
        module = self.compatibility_module("capabilities")
        if operation.startswith("_") or not callable(getattr(module, operation, None)):
            raise UnsupportedPrinterOperation(f"Unsupported Bambu feature policy {operation!r}")
        function = getattr(module, operation)
        if operation in {"supports_drying", "supports_drying_while_printing"}:
            return function(model, options.get("firmware"))
        if operation == "display_temperatures":
            return function(options.get("temperatures"), model)
        return function(model)

    async def recover_connection(self, config: PrinterConfig, connection: Any, offline_grace: float) -> bool | None:
        import logging
        import time

        from .diagnostic import PORT_MQTT, check_port

        logger = logging.getLogger("backend.app.services.printer_manager")
        last_message = connection._last_message_time
        if not last_message or time.time() - last_message < offline_grace:
            return None
        offline_for = time.time() - last_message
        if not await check_port(config.ip_address, PORT_MQTT):
            logger.debug("Printer %s MQTT port is not answering; leaving reconnect to paho", config.printer_id)
            return False
        logger.warning(
            "[#2732] Printer %s has been offline for %.0fs but answers on MQTT port %d — "
            "rebuilding the client with a fresh session (last connect error: %s)",
            config.printer_id,
            offline_for,
            PORT_MQTT,
            connection.last_connect_error or "none recorded",
        )
        try:
            connection.force_reconnect_stale_session(f"offline for {offline_for:.0f}s, port still answering")
        except Exception:
            logger.exception("Connection recovery failed for printer %s", config.printer_id)
        return True

    def create_connection(self, config: PrinterConfig, **callbacks: Any):
        from .mqtt import BambuMQTTClient

        self.validate_config(config)
        return BambuMQTTClient(
            ip_address=config.ip_address,
            serial_number=config.serial_number,
            access_code=config.access_code,
            model=config.model,
            **callbacks,
        )

    def create_file_client(self, config: PrinterConfig, **options: Any):
        from .ftp import BambuFTPClient

        self.validate_config(config)
        return BambuFTPClient(config.ip_address, config.access_code, printer_model=config.model, **options)

    def loaded_materials(self, state):
        from .materials import loaded_materials

        return loaded_materials(state, self.normalize_telemetry(state))

    def normalize_telemetry(self, state):
        # Bambu's ingestion layer already normalizes AMS and tray identifiers.
        # Returning the existing core schema preserves partial-push semantics.
        return state.raw_data or {}

    def status_fingerprint(self, state):
        from .workflows import status_fingerprint

        return status_fingerprint(state)

    def archive_filament_metadata(self, data, mapping=None):
        from .workflows import _extract_filament_data_from_mqtt

        return _extract_filament_data_from_mqtt(data, mapping)

    def derive_failure_reason(self, status, data):
        from .workflows import derive_failure_reason

        return derive_failure_reason(status, data.get("hms_errors"))

    async def cleanup_after_print(self, printer_id, data, archive_id, logger):
        from .workflows import _cleanup_sd_card_after_print

        await _cleanup_sd_card_after_print(printer_id, data.get("subtask_name"), archive_id, logger)

    def derive_status(self, state, model=None):
        from .serializer import get_derived_status_name

        return get_derived_status_name(state, model)

    def serialize_state(self, state, printer_id=None, model=None, drying_targets=None):
        from .serializer import printer_state_to_dict

        return printer_state_to_dict(state, printer_id, model, drying_targets)

    def command_handlers(self, connection):
        # Lookup is restricted to the adapter's immutable public allowlist.
        # No caller-provided name can access a raw socket or private method.
        return {
            name: getattr(connection, name) for name in self.operations if callable(getattr(connection, name, None))
        }

    def file_operation(self, operation: str, *args: Any, **kwargs: Any):
        from . import ftp

        if operation not in _FILE_OPERATIONS:
            raise UnsupportedPrinterOperation(f"Bambu file operation {operation!r} is not exposed")
        return getattr(ftp, operation)(*args, **kwargs)

    def camera_operation(self, operation: str, *args: Any, **kwargs: Any):
        from . import camera, camera_profiles

        if operation == "diagnose_camera":
            from .camera_diagnose import diagnose_camera

            return diagnose_camera(*args, **kwargs)
        if operation in {"stream", "stream_fps"}:
            from . import streams

            return getattr(streams, operation)(*args, **kwargs)
        if operation not in _CAMERA_OPERATIONS:
            raise UnsupportedPrinterOperation(f"Bambu camera operation {operation!r} is not exposed")
        module = camera_profiles if operation == "get_camera_profile" else camera
        return getattr(module, operation)(*args, **kwargs)

    def discovery_operation(self, operation: str, *args: Any, **kwargs: Any):
        from . import discovery

        if operation == "service":
            return discovery.discovery_service
        if operation == "subnet_scanner":
            return discovery.subnet_scanner
        raise UnsupportedPrinterOperation(f"Bambu discovery operation {operation!r} is not exposed")

    def diagnostic_operation(self, operation: str, *args: Any, **kwargs: Any):
        if operation == "connection":
            from .diagnostic import run_connection_diagnostic

            return run_connection_diagnostic(*args, **kwargs)
        raise UnsupportedPrinterOperation(f"Bambu diagnostic operation {operation!r} is not exposed")

    def storage_operation(self, operation: str, *args: Any, **kwargs: Any):
        from . import storage

        if operation not in {
            "print_file_reachable_over_ftp",
            "last_print_storage_verdict",
            "external_storage_present",
            "ftp_probe_paths",
            "probe_filename_from_url",
            "url_is_external_storage",
        }:
            raise UnsupportedPrinterOperation(f"Bambu storage operation {operation!r} is not exposed")
        kwargs.pop("state", None)
        return getattr(storage, operation)(*args, **kwargs)

    def firmware_operation(self, operation: str, *args: Any, **kwargs: Any):
        if operation in {"get_upload_state", "upload_in_progress", "reset_upload_state"}:
            from . import firmware_update

            return getattr(firmware_update, operation)(*args, **kwargs)
        if operation == "service":
            from .firmware_update import get_firmware_update_service

            return get_firmware_update_service()
        if operation == "check_service":
            from .firmware_check import get_firmware_service

            return get_firmware_service()
        raise UnsupportedPrinterOperation(f"Bambu firmware operation {operation!r} is not exposed")

    def cloud_operation(self, operation: str, *args: Any, **kwargs: Any):
        if operation == "create_service":
            from .cloud import BambuCloudService

            return BambuCloudService(*args, **kwargs)
        raise UnsupportedPrinterOperation(f"Bambu cloud operation {operation!r} is not exposed")

    def virtual_printer_service(self):
        from .virtual_printer.manager import virtual_printer_manager

        return virtual_printer_manager

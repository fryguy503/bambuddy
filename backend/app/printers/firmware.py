"""Firmware services and shared upload state exposed by printer addons."""

from dataclasses import dataclass

from backend.app.core.compat import StrEnum
from backend.app.printers.api import config_for_id
from backend.app.printers.registry import get_plugin, plugin_for


class FirmwareUploadStatus(StrEnum):
    """Status of a firmware upload operation."""

    IDLE = "idle"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    UPLOADING = "uploading"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class FirmwareUploadState:
    """State of a firmware upload operation for a printer."""

    status: FirmwareUploadStatus = FirmwareUploadStatus.IDLE
    progress: int = 0  # 0-100
    message: str = ""
    error: str | None = None
    firmware_filename: str | None = None
    firmware_version: str | None = None


def get_firmware_service(printer=None, *, plugin_id: str = "bambu"):
    plugin = plugin_for(printer) if printer is not None else get_plugin(plugin_id)
    return plugin.firmware_operation("check_service")


def get_firmware_update_service(printer=None, *, plugin_id: str = "bambu"):
    plugin = plugin_for(printer) if printer is not None else get_plugin(plugin_id)
    return plugin.firmware_operation("service")


def _upload_plugin(printer_id: int, plugin_id: str | None = None):
    config = config_for_id(printer_id)
    return get_plugin(plugin_id or (config.plugin_id if config else "bambu"))


def _supports_firmware(plugin, printer_id: int) -> bool:
    config = config_for_id(printer_id)
    capabilities = plugin.model_capabilities(config.model) if config is not None else plugin.capabilities
    return "firmware" in capabilities


def get_upload_state(printer_id: int, *, plugin_id: str | None = None) -> FirmwareUploadState:
    plugin = _upload_plugin(printer_id, plugin_id)
    if not _supports_firmware(plugin, printer_id):
        return FirmwareUploadState()
    return plugin.firmware_operation("get_upload_state", printer_id)


def upload_in_progress(printer_id: int, *, plugin_id: str | None = None) -> bool:
    plugin = _upload_plugin(printer_id, plugin_id)
    if not _supports_firmware(plugin, printer_id):
        return False
    return plugin.firmware_operation("upload_in_progress", printer_id)


def reset_upload_state(printer_id: int, *, plugin_id: str | None = None) -> None:
    plugin = _upload_plugin(printer_id, plugin_id)
    if _supports_firmware(plugin, printer_id):
        plugin.firmware_operation("reset_upload_state", printer_id)

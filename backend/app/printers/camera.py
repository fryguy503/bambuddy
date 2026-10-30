"""Printer camera operations dispatched through the configured addon."""

from pathlib import Path
from typing import Any

from backend.app.printers.api import config_for_ip, plugin_for_ip
from backend.app.printers.contracts import PrinterPluginError


def _dispatch(
    operation: str, address: str, *args: Any, plugin_id: str | None = None, printer_id: int | None = None, **kwargs: Any
):
    if not isinstance(address, str):
        raise TypeError("Printer camera operation requires an address string or a configured addressless printer")
    if not address and printer_id is None:
        raise PrinterPluginError("Addressless transports require an explicit configured printer_id")
    config = config_for_ip(address, plugin_id=plugin_id, printer_id=printer_id)
    if not address and config is None:
        raise PrinterPluginError("Addressless transport has no configured printer binding")
    plugin = plugin_for_ip(address, plugin_id=plugin_id, printer_id=printer_id)
    if config is not None:
        return plugin.camera_operation_for(config, operation, *args, **kwargs)
    return plugin.camera_operation(operation, *args, **kwargs)


async def capture_camera_frame_bytes(
    ip_address: str,
    access_code: str,
    model: str | None,
    timeout: int = 15,
    *,
    plugin_id: str | None = None,
    printer_id: int | None = None,
) -> bytes | None:
    return await _dispatch(
        "capture_camera_frame_bytes",
        ip_address,
        ip_address,
        access_code,
        model,
        timeout,
        plugin_id=plugin_id,
        printer_id=printer_id,
    )


async def capture_camera_frame(
    ip_address: str,
    access_code: str,
    model: str | None,
    output_path: Path,
    timeout: int = 30,
    *,
    plugin_id: str | None = None,
    printer_id: int | None = None,
) -> bool:
    return await _dispatch(
        "capture_camera_frame",
        ip_address,
        ip_address,
        access_code,
        model,
        output_path,
        timeout,
        plugin_id=plugin_id,
        printer_id=printer_id,
    )


async def capture_finish_photo(
    printer_id: int,
    ip_address: str,
    access_code: str,
    model: str | None,
    archive_dir: Path,
    rotation: int = 0,
    *,
    plugin_id: str | None = None,
) -> str | None:
    return await _dispatch(
        "capture_finish_photo",
        ip_address,
        printer_id,
        ip_address,
        access_code,
        model,
        archive_dir,
        rotation,
        plugin_id=plugin_id,
        printer_id=printer_id,
    )


def capture_in_flight(ip_address: str, *, plugin_id: str | None = None, printer_id: int | None = None) -> bool:
    return _dispatch("capture_in_flight", ip_address, ip_address, plugin_id=plugin_id, printer_id=printer_id)


async def test_camera_connection(
    ip_address: str, access_code: str, model: str | None, *, plugin_id: str | None = None, printer_id: int | None = None
) -> dict:
    return await _dispatch(
        "test_camera_connection", ip_address, ip_address, access_code, model, plugin_id=plugin_id, printer_id=printer_id
    )


async def diagnose_camera(*, ip_address: str, plugin_id: str | None = None, printer_id: int | None = None, **options):
    return await _dispatch(
        "diagnose_camera", ip_address, ip_address=ip_address, plugin_id=plugin_id, printer_id=printer_id, **options
    )

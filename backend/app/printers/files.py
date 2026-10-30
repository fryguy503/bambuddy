"""Vendor-neutral file service dispatch.

Network operations resolve the configured addon by printer address, or accept
an explicit ``plugin_id`` keyword. Legacy unbound calls use the bundled Bambu
adapter; a configured non-Bambu address never falls through to Bambu.
"""

from __future__ import annotations

from typing import Any

from backend.app.printers import file_helpers
from backend.app.printers.api import (
    bind_printer as bind_printer,
    config_for_ip,
    leased_call,
    plugin_for_ip,
    unbind_printer as unbind_printer,
)
from backend.app.printers.contracts import PrinterConfig, PrinterPluginError, UnsupportedPrinterOperation
from backend.app.printers.file_types import (
    DeleteResult as DeleteResult,
    DownloadCancelled as DownloadCancelled,
    DownloadDeadlineExceeded as DownloadDeadlineExceeded,
    DownloadInsufficientSpace as DownloadInsufficientSpace,
    DownloadLimitExceeded as DownloadLimitExceeded,
    FileListResult as FileListResult,
    FileNotOnPrinterError as FileNotOnPrinterError,
    FtpFailure as FtpFailure,
    FtpFailureKind as FtpFailureKind,
    FtpFailureReport as FtpFailureReport,
    UploadCancelled as UploadCancelled,
)
from backend.app.printers.registry import plugin_for

# Application recovery-download budget, shared across file providers.
RECOVERY_DOWNLOAD_MAX_TIMEOUT = 900.0


def create_file_client(printer: Any, **options: Any):
    config = PrinterConfig.from_printer(printer)
    return plugin_for(config).create_file_client(config, **options)


def _dispatch(operation: str, *args: Any, **kwargs: Any) -> Any:
    plugin_id = kwargs.pop("plugin_id", None)
    printer_id = kwargs.pop("printer_id", None)
    ip_address = args[0] if args else kwargs.get("ip_address")
    if not isinstance(ip_address, str):
        raise TypeError(f"Printer file operation {operation!r} requires an ip_address")
    if not ip_address and printer_id is None:
        raise PrinterPluginError("Addressless transports require an explicit configured printer_id")
    config = config_for_ip(ip_address, plugin_id=plugin_id, printer_id=printer_id)
    if not ip_address and config is None:
        raise PrinterPluginError("Addressless transport has no configured printer binding")
    plugin = plugin_for_ip(ip_address, plugin_id=plugin_id, printer_id=printer_id)
    if config is not None:
        return leased_call(config, plugin.file_operation_for, config, operation, *args, **kwargs)
    return plugin.file_operation(operation, *args, **kwargs)


async def download_file_async(*args: Any, **kwargs: Any):
    return await _dispatch("download_file_async", *args, **kwargs)


async def download_file_try_paths_async(*args: Any, **kwargs: Any):
    return await _dispatch("download_file_try_paths_async", *args, **kwargs)


async def upload_file_async(*args: Any, **kwargs: Any):
    return await _dispatch("upload_file_async", *args, **kwargs)


async def list_files_async(*args: Any, **kwargs: Any):
    return await _dispatch("list_files_async", *args, **kwargs)


async def list_files_result_async(*args: Any, **kwargs: Any):
    return await _dispatch("list_files_result_async", *args, **kwargs)


async def find_remote_file_async(*args: Any, **kwargs: Any):
    return await _dispatch("find_remote_file_async", *args, **kwargs)


async def delete_file_async(*args: Any, **kwargs: Any):
    return await _dispatch("delete_file_async", *args, **kwargs)


async def download_file_bytes_async(*args: Any, **kwargs: Any):
    return await _dispatch("download_file_bytes_async", *args, **kwargs)


async def remote_file_settled(*args: Any, **kwargs: Any):
    return await _dispatch("remote_file_settled", *args, **kwargs)


async def delete_archived_timelapse(*args: Any, **kwargs: Any):
    return await _dispatch("delete_archived_timelapse", *args, **kwargs)


async def get_storage_info_async(*args: Any, **kwargs: Any):
    return await _dispatch("get_storage_info_async", *args, **kwargs)


def ftps_handshake_blocked(*args: Any, **kwargs: Any) -> bool:
    try:
        return _dispatch("ftps_handshake_blocked", *args, **kwargs)
    except UnsupportedPrinterOperation:
        # A transport without FTPS has no FTPS handshake cooldown.
        return False


def normalize_3mf_name(*args: Any, **kwargs: Any):
    kwargs.pop("plugin_id", None)
    return file_helpers.normalize_3mf_name(*args, **kwargs)


def describe_upload_failure(*args: Any, **kwargs: Any):
    kwargs.pop("plugin_id", None)
    return file_helpers.describe_upload_failure(*args, **kwargs)


async def get_ftp_retry_settings(*args: Any, **kwargs: Any):
    kwargs.pop("plugin_id", None)
    return await file_helpers.get_ftp_retry_settings(*args, **kwargs)


async def with_ftp_retry(*args: Any, **kwargs: Any):
    plugin_id = kwargs.pop("plugin_id", None)
    printer_id = kwargs.get("printer_id")
    return await file_helpers.with_ftp_retry(
        *args,
        _cooloff_check=lambda ip: ftps_handshake_blocked(ip, plugin_id=plugin_id, printer_id=printer_id),
        **kwargs,
    )


def cache_3mf_download(*args: Any, **kwargs: Any):
    kwargs.pop("plugin_id", None)
    return file_helpers.cache_3mf_download(*args, **kwargs)


def get_cached_3mf(*args: Any, **kwargs: Any):
    kwargs.pop("plugin_id", None)
    return file_helpers.get_cached_3mf(*args, **kwargs)


def clear_3mf_cache(*args: Any, **kwargs: Any):
    kwargs.pop("plugin_id", None)
    return file_helpers.clear_3mf_cache(*args, **kwargs)

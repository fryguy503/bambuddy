"""Normalized printer file reachability, independent of transport or storage media."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.app.printers.api import config_for_id
from backend.app.printers.contracts import PrinterPluginError
from backend.app.printers.registry import get_plugin, plugin_for

# Stable compatibility reasons consumed by existing archive and recovery views.
REASON_INTERNAL_STORAGE = "internal_storage"
REASON_INTERNAL_HISTORY = "internal_history"
REASON_NO_EXTERNAL_STORAGE = "no_external_storage"
REASON_FTPS_COOLOFF = "ftps_cooloff"
REASON_FTP_TRANSFER_FAILED = "ftp_transfer_failed"


@dataclass(frozen=True)
class StorageVerdict:
    reachable: bool
    reason: str | None = None
    probe_filename: str | None = None


def _metadata(state: object | None) -> Mapping:
    metadata = state.get("storage") if isinstance(state, Mapping) else getattr(state, "storage", None)
    return metadata if isinstance(metadata, Mapping) else {}


def _safe_probe_filename(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 255 or value.startswith("."):
        return None
    if "/" in value or "\\" in value or any(character < " " or character == "\x7f" for character in value):
        return None
    return value


def generic_storage_operation(operation: str, *args: Any, **kwargs: Any):
    """Default policy uses only metadata the provider explicitly normalizes.

    No URL scheme, model family, removable-media flag or FTP directory implies
    reachability for another printer addon. Unknown remains worth attempting.
    """
    state = kwargs.get("state") if operation == "ftp_probe_paths" else (args[0] if args else None)
    metadata = _metadata(state)
    if operation in {"print_file_reachable_over_ftp", "last_print_storage_verdict"}:
        if metadata.get("reachable") is not False:
            return StorageVerdict(reachable=True)
        reason = metadata.get("reason")
        return StorageVerdict(
            reachable=False,
            reason=reason if isinstance(reason, str) and reason else "file_unavailable",
            probe_filename=_safe_probe_filename(metadata.get("probe_filename")),
        )
    if operation == "external_storage_present":
        return metadata.get("external_present") is not False
    if operation == "ftp_probe_paths":
        paths = metadata.get("probe_paths")
        if isinstance(paths, (list, tuple)) and all(isinstance(path, str) and path for path in paths):
            return list(paths)
        return [args[0]]
    if operation in {"url_is_external_storage", "probe_filename_from_url"}:
        return None
    raise PrinterPluginError(f"Unknown printer storage operation {operation!r}")


def _dispatch(operation: str, *args: Any, printer_id: int | None = None, plugin_id: str | None = None, **kwargs: Any):
    config = config_for_id(printer_id)
    state = kwargs.get("state") if operation == "ftp_probe_paths" else (args[0] if args else None)
    state_plugin_id = state.get("plugin_id") if isinstance(state, Mapping) else getattr(state, "plugin_id", None)
    selected_id = plugin_id or (config.plugin_id if config is not None else None)
    if selected_id is None and isinstance(state_plugin_id, str):
        selected_id = state_plugin_id
    plugin = plugin_for(config) if config is not None else get_plugin(selected_id or "bambu")
    if config is not None and (plugin_id is None or plugin_id == config.plugin_id):
        return plugin.storage_operation_for(config, operation, *args, **kwargs)
    return plugin.storage_operation(operation, *args, **kwargs)


def print_file_reachable_over_ftp(state: object | None, *, printer_id=None, plugin_id=None) -> StorageVerdict:
    """Compatibility name for the configured addon's current-print file verdict."""
    return _dispatch("print_file_reachable_over_ftp", state, printer_id=printer_id, plugin_id=plugin_id)


def last_print_storage_verdict(state: object | None, *, printer_id=None, plugin_id=None) -> StorageVerdict:
    return _dispatch("last_print_storage_verdict", state, printer_id=printer_id, plugin_id=plugin_id)


def external_storage_present(state: object | None, *, printer_id=None, plugin_id=None) -> bool:
    return _dispatch("external_storage_present", state, printer_id=printer_id, plugin_id=plugin_id)


def ftp_probe_paths(filename: str, *, printer_id=None, plugin_id=None, state=None) -> list[str]:
    options = {"state": state} if state is not None else {}
    return _dispatch("ftp_probe_paths", filename, printer_id=printer_id, plugin_id=plugin_id, **options)


def url_is_external_storage(project_url: str | None, *, printer_id=None, plugin_id=None) -> bool | None:
    return _dispatch("url_is_external_storage", project_url, printer_id=printer_id, plugin_id=plugin_id)


def probe_filename_from_url(project_url: str | None, *, printer_id=None, plugin_id=None) -> str | None:
    return _dispatch("probe_filename_from_url", project_url, printer_id=printer_id, plugin_id=plugin_id)

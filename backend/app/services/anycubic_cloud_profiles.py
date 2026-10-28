"""Read-only Anycubic cloud slicer-preset archive decoding.

Native ACSN 2.0.0.3 uses POST /v3/user/getSlicerConfig with an empty body,
then downloads data.url as a ZIP of user preset files. This is unrelated to
cloud print-file /profile endpoints and ACE material inventory. The native
get/set/pre-sign and ZIP paths were statically traced; no vendor code executes.
The transport owns authentication, allowed download origins and byte limits.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import stat
import zipfile
from pathlib import PurePosixPath
from typing import Any, Protocol

from backend.app.services.anycubic_cloud import AnycubicCloudAuthError

CONFIG_ROUTE = "/v3/user/getSlicerConfig"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 2000
_KINDS = {"filament": "filament", "machine": "printer", "printer": "printer", "process": "process", "print": "process"}


class AnycubicProfileError(ValueError):
    """A fixed diagnostic without account tokens, signed URLs or preset contents."""


class ProfileTransport(Protocol):
    @property
    def account_namespace(self) -> str: ...

    async def validate_token(self) -> dict[str, str]: ...

    async def request(self, method: str, path: str) -> dict[str, Any]: ...

    async def download_config(self, url: str) -> bytes: ...


def _text(value: Any, *, maximum: int = 1024) -> str | None:
    if isinstance(value, str) and value and len(value) <= maximum and not any(ord(c) < 32 for c in value):
        return value
    return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AnycubicProfileError("Preset JSON contains duplicate keys")
        result[key] = value
    return result


def _invalid_number(value: str) -> None:
    raise AnycubicProfileError("Preset JSON contains an invalid number")


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise AnycubicProfileError("Preset JSON exceeds its nesting limit")
    if isinstance(value, dict):
        if len(value) > 20000 or any(not isinstance(key, str) or len(key) > 1024 for key in value):
            raise AnycubicProfileError("Preset JSON exceeds its field limit")
        for child in value.values():
            _validate_json(child, depth + 1)
        for key in value:
            key.encode("utf-8")
    elif isinstance(value, list):
        if len(value) > 20000:
            raise AnycubicProfileError("Preset JSON exceeds its array limit")
        for child in value:
            _validate_json(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise AnycubicProfileError("Preset JSON contains an invalid number")
    elif isinstance(value, str):
        value.encode("utf-8")  # Reject lone surrogates before HTTP JSON serialization.


def _path(entry: zipfile.ZipInfo) -> PurePosixPath:
    name = entry.orig_filename
    if (
        not name
        or len(name) > 1024
        or name != entry.filename
        or "\\" in name
        or ":" in name
        or any(ord(c) < 32 for c in name)
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
    ):
        raise AnycubicProfileError("Preset archive contains an unsafe path")
    return PurePosixPath(name)


def _source_identity(setting: dict[str, Any]) -> tuple[str | None, str | None, Any]:
    """Preserve a server setting ID's JSON type rather than conflating 7 and '7'."""
    source = setting.get("setting_id")
    if type(source) is int:
        return str(source), "integer", source
    if _text(source, maximum=512) is not None:
        return source, "string", source
    if source is not None:
        raise AnycubicProfileError("Preset has an invalid source identifier")
    return None, None, None


def _profile(setting: dict[str, Any], path: PurePosixPath, namespace: str) -> dict[str, Any] | None:
    raw_kind = setting.get("type")
    if raw_kind is not None and _text(raw_kind, maximum=64) is None:
        raise AnycubicProfileError("Preset has an invalid type")
    folder_kinds = {_KINDS[part.lower()] for part in path.parts[:-1] if part.lower() in _KINDS}
    if len(folder_kinds) > 1:
        raise AnycubicProfileError("Preset archive has an ambiguous type directory")
    folder_kind = next(iter(folder_kinds), None)
    if raw_kind is None and folder_kind is None:
        return None  # Named application metadata outside preset directories is not a profile.
    declared_kind = _KINDS.get(raw_kind.lower()) if isinstance(raw_kind, str) else None
    if declared_kind and folder_kind and declared_kind != folder_kind:
        raise AnycubicProfileError("Preset type does not match its archive directory")
    kind = declared_kind or (folder_kind if raw_kind is None else None) or "unsupported"
    name = _text(setting.get("name"), maximum=300)
    if name is None:
        if setting.get("name") is not None:
            raise AnycubicProfileError("Preset has an invalid name")
        # The native archive is a directory tree of preset JSONs. A typed file
        # can use its native filename; untyped application JSON is not a preset.
        if kind == "unsupported":
            return None
        if not declared_kind and not any(
            key in setting
            for key in (
                "inherits",
                "filament_type",
                "printer_model",
                "nozzle_diameter",
                "layer_height",
                "filament_settings_id",
                "print_settings_id",
                "printer_settings_id",
            )
        ):
            return None
        name = _text(path.stem, maximum=300)
        if name is None:
            raise AnycubicProfileError("Preset has an invalid filename")
    source_id, source_type, source_value = _source_identity(setting)
    identity = json.dumps(
        [namespace, path.as_posix(), source_type, source_value], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    result: dict[str, Any] = {
        "setting_id": "anycubic_" + hashlib.sha256(identity).hexdigest(),
        "name": name,
        "type": raw_kind if kind == "unsupported" and raw_kind is not None else kind,
        "setting": setting,
        "source_path": path.as_posix(),
        "source": "anycubic",
    }
    if raw_kind is not None:
        result["source_type"] = raw_kind
    if source_id is not None:
        result["source_id"] = source_id
        result["source_id_type"] = source_type
    # No inheritance resolution against another manufacturer's catalog: retain
    # the original settings and report their actual metadata only.
    for key in ("version", "inherits"):
        if _text(setting.get(key), maximum=300) is not None:
            result[key] = setting[key]
    for key in ("updated_time", "update_time"):
        value = setting.get(key)
        if isinstance(value, str) or (type(value) is int and value >= 0):
            result["updated_time"] = value
            break
    return result


def parse_config_archive(archive: bytes, account_namespace: str) -> list[dict[str, Any]]:
    """Read bounded preset JSONs in memory; never extract files or execute G-code."""
    if not isinstance(archive, bytes) or not 0 < len(archive) <= MAX_ARCHIVE_BYTES:
        raise AnycubicProfileError("Preset archive is empty or exceeds its size limit")
    if _text(account_namespace) is None:
        raise AnycubicProfileError("A validated cloud account is required")
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    expanded = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_ENTRIES:
                raise AnycubicProfileError("Preset archive exceeds its entry limit")
            # Validate every member before reading any JSON, including files
            # which are not presets. No ambiguous partial snapshot is returned.
            for entry in entries:
                path = _path(entry)
                folded = path.as_posix().casefold()
                if folded in seen:
                    raise AnycubicProfileError("Preset archive contains duplicate paths")
                seen.add(folded)
                file_type = stat.S_IFMT(entry.external_attr >> 16)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR} or entry.flag_bits & 1:
                    raise AnycubicProfileError("Preset archive contains an unsupported entry")
                if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise AnycubicProfileError("Preset archive uses an unsupported compression method")
                expanded += entry.file_size
                if entry.file_size > MAX_FILE_BYTES or expanded > MAX_EXPANDED_BYTES:
                    raise AnycubicProfileError("Preset archive exceeds its expanded size limit")
            for entry in entries:
                path = _path(entry)
                if entry.is_dir() or path.suffix.lower() != ".json":
                    continue
                with bundle.open(entry) as member:
                    raw = member.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES or len(raw) != entry.file_size:
                    raise AnycubicProfileError("Preset file exceeds its size limit")
                setting = json.loads(
                    raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_invalid_number
                )
                if not isinstance(setting, dict):
                    raise AnycubicProfileError("Preset JSON must contain an object")
                _validate_json(setting)
                profile = _profile(setting, path, account_namespace)
                if profile is not None:
                    profiles.append(profile)
    except AnycubicProfileError:
        raise
    except (ValueError, UnicodeError, OSError, RuntimeError, RecursionError, zipfile.BadZipFile, NotImplementedError):
        raise AnycubicProfileError("Preset archive could not be decoded") from None
    return sorted(profiles, key=lambda item: (item["type"], item["name"].casefold(), item["setting_id"]))


async def load_profiles(service: ProfileTransport) -> list[dict[str, Any]]:
    """Retrieve only the current account's config archive, then decode its presets."""
    try:
        namespace = service.account_namespace
    except AnycubicCloudAuthError:
        await service.validate_token()
        namespace = service.account_namespace
    if _text(namespace) is None:
        raise AnycubicProfileError("A validated cloud account is required")
    response = await service.request("POST", CONFIG_ROUTE)
    if type(response.get("code")) is not int or response["code"] != 1:
        raise AnycubicProfileError("Cloud slicer configuration request was rejected")
    data = response.get("data")
    if data is None:
        return []
    if not isinstance(data, dict):
        raise AnycubicProfileError("Cloud slicer configuration has an unsupported shape")
    url = data.get("url")
    if url in (None, ""):
        return []
    if not isinstance(url, str):
        raise AnycubicProfileError("Cloud slicer configuration has an invalid download reference")
    archive = await service.download_config(url)
    if service.account_namespace != namespace:
        raise AnycubicProfileError("Cloud account changed while reading presets")
    return parse_config_archive(archive, namespace)

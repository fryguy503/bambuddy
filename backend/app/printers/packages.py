"""Static, bounded printer package inspection and version-isolated loading.

Checksums prove integrity, not publisher authenticity. Optional Ed25519
signatures are checked against keys explicitly trusted by an administrator.
No package code is imported while inspecting or staging an upload.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import importlib
import importlib.metadata
import importlib.util
import io
import json
import re
import stat
import sys
import types
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from backend.app.core.config import APP_VERSION
from backend.app.core.permissions import Permission
from backend.app.printers.contracts import PrinterPlugin, PrinterPluginError
from backend.app.printers.sdk import (
    SDK_API_VERSION,
    PluginManifest,
    check_manifest_compatibility,
    manifest_permissions,
    validate_manifest,
)

MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_FILES = 2048
MAX_MANIFEST_BYTES = 512 * 1024
SUPPORTED_APIS = ("1.0.0", SDK_API_VERSION)
_NAMESPACE = "bambuddy_printer_plugins"
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class PluginPackageError(PrinterPluginError):
    pass


def safe_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value or value.startswith("/"):
        raise PluginPackageError("Package contains an invalid relative path")
    parts = value.split("/")
    if any(
        part in {"", ".", ".."}
        or any(character in part for character in '<>:"|?*')
        or any(ord(character) < 32 for character in part)
        or part.rstrip(" .") != part
        or part.split(".", 1)[0].upper() in _RESERVED
        for part in parts
    ):
        raise PluginPackageError("Package path is not portable or escapes its payload")
    return PurePosixPath(value)


def canonical_manifest(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PluginPackageError("Package JSON contains duplicate keys")
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PluginPackageError("Package metadata must be UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise PluginPackageError("Package metadata must be a JSON object")
    return result


def installed_dependency_versions(manifest: PluginManifest) -> dict[str, str]:
    installed = {}
    for name in manifest.dependencies:
        try:
            installed[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return installed


def validate_compatibility(manifest: PluginManifest) -> None:
    check_manifest_compatibility(
        manifest,
        core_version=APP_VERSION,
        supported_api_versions=SUPPORTED_APIS,
        installed_dependencies=installed_dependency_versions(manifest),
    )


@dataclass(frozen=True)
class InspectedPackage:
    manifest: PluginManifest
    manifest_json: dict
    files: dict[str, bytes]
    sha256: str
    signature: dict | None
    signer_key_id: str | None

    @property
    def validation(self) -> dict:
        return {
            "manifest": True,
            "compatibility": True,
            "inventory": True,
            "python_syntax": True,
            "signature_verified": self.signer_key_id is not None,
            "trust": "trusted-publisher-key" if self.signer_key_id else "administrator-installed-code",
            "hardware_certified": False,
        }


def inspect_package(
    content: bytes,
    *,
    trusted_keys: dict[str, dict] | None = None,
    expected_sha256: str | None = None,
) -> InspectedPackage:
    if not content or len(content) > MAX_PACKAGE_BYTES:
        raise PluginPackageError("Printer package exceeds the 64 MiB upload limit or is empty")
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and (
        not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)
        or not hmac.compare_digest(expected_sha256.lower(), digest)
    ):
        raise PluginPackageError("Printer package SHA-256 does not match")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise PluginPackageError("Printer package must be a ZIP archive") from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_PACKAGE_FILES:
            raise PluginPackageError("Printer package contains too many entries")
        expanded = 0
        seen = set()
        payload = {}
        manifest_bytes = None
        signature_bytes = None
        for info in members:
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            safe_relative_path(name)
            if name.casefold() in seen:
                raise PluginPackageError("Printer package contains duplicate or case-colliding paths")
            seen.add(name.casefold())
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                raise PluginPackageError("Printer packages cannot contain links or special files")
            if info.flag_bits & 1:
                raise PluginPackageError("Encrypted printer packages are not supported")
            if info.is_dir():
                if name != "payload" and not name.startswith("payload/"):
                    raise PluginPackageError("Unexpected package directory")
                continue
            expanded += info.file_size
            if info.file_size > MAX_FILE_BYTES or expanded > MAX_EXPANDED_BYTES:
                raise PluginPackageError("Printer package exceeds expanded file limits")
            if info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * 250:
                raise PluginPackageError("Printer package compression ratio exceeds the allowed limit")
            if name not in {"manifest.json", "signature.json"} and not name.startswith("payload/"):
                raise PluginPackageError("Only manifest.json, signature.json and payload files are allowed")
            try:
                with archive.open(info) as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                raise PluginPackageError("Printer package data failed its archive integrity check") from exc
            if len(data) != info.file_size or len(data) > MAX_FILE_BYTES:
                raise PluginPackageError("Printer package entry size does not match its metadata")
            if name == "manifest.json":
                manifest_bytes = data
            elif name == "signature.json":
                signature_bytes = data
            else:
                relative = name.removeprefix("payload/")
                safe_relative_path(relative)
                if Path(relative).suffix.lower() in {".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe"}:
                    raise PluginPackageError(
                        "Package Python source and resources; native dependencies must be preinstalled"
                    )
                payload[relative] = data
    if manifest_bytes is None or len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise PluginPackageError("Printer package requires a bounded manifest.json")
    raw_manifest = _json(manifest_bytes)
    try:
        manifest = validate_manifest(raw_manifest)
        validate_compatibility(manifest)
        known_permissions = {permission.value for permission in Permission}
        if manifest_permissions(manifest) - known_permissions:
            raise PluginPackageError("Printer package declares permissions this core does not support")
    except (ValueError, TypeError, RecursionError) as exc:
        raise PluginPackageError(
            f"Printer package manifest is incompatible or invalid: {str(exc).splitlines()[0][:400]}"
        ) from exc
    if set(payload) != set(manifest.files):
        raise PluginPackageError("Printer package inventory must name every payload file exactly")
    if "__init__.py" not in payload:
        raise PluginPackageError("Printer package payload requires __init__.py")
    for name, data in payload.items():
        safe_relative_path(name)
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), manifest.files[name].lower()):
            raise PluginPackageError(f"Printer package file checksum does not match: {name}")
        if name.endswith(".py"):
            try:
                tree = ast.parse(data.decode("utf-8-sig"), filename=name)
                compile(tree, filename=name, mode="exec", dont_inherit=True)
            except (SyntaxError, UnicodeDecodeError, RecursionError) as exc:
                raise PluginPackageError(f"Printer package Python source is invalid: {name}") from exc
            for node in ast.walk(tree):
                imports = (
                    [item.name for item in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom) and not node.level
                    else []
                )
                if any(
                    module.startswith(
                        (
                            "backend.app.printers.bambu",
                            "backend.app.services.bambu_",
                            "backend.app.services.virtual_printer",
                        )
                    )
                    for module in imports
                ):
                    raise PluginPackageError("Plugin payload must use relative imports for its own implementation")
    if signature_bytes is not None and len(signature_bytes) > 8192:
        raise PluginPackageError("Printer package signature metadata exceeds its size limit")
    signature = _json(signature_bytes) if signature_bytes is not None else None
    signer = None
    if signature is not None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        signer = signature.get("key_id")
        trusted = (trusted_keys or {}).get(signer, {}) if isinstance(signer, str) else {}
        if (
            signature.get("algorithm") != "ed25519"
            or trusted.get("publisher_id") != manifest.publisher_id
            or trusted.get("revoked", False)
        ):
            raise PluginPackageError("Printer package signer is not a trusted key for this publisher")
        try:
            public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted["public_key"], validate=True))
            public_key.verify(base64.b64decode(signature["signature"], validate=True), canonical_manifest(raw_manifest))
        except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
            raise PluginPackageError("Printer package publisher signature is invalid") from exc
    return InspectedPackage(manifest, raw_manifest, payload, digest, signature, signer)


def load_package_plugin(payload: Path, manifest: PluginManifest, package_sha256: str) -> PrinterPlugin:
    """Execute a previously validated, administrator-selected package at startup."""
    validate_compatibility(manifest)
    if _NAMESPACE not in sys.modules:
        parent = types.ModuleType(_NAMESPACE)
        parent.__path__ = []
        sys.modules[_NAMESPACE] = parent
    identifier = re.sub(r"[^a-zA-Z0-9_]", "_", manifest.plugin_id)
    namespace = f"{_NAMESPACE}.{identifier}_{package_sha256[:20]}"
    spec = importlib.util.spec_from_file_location(
        namespace, payload / "__init__.py", submodule_search_locations=[str(payload)]
    )
    if spec is None or spec.loader is None:
        raise PluginPackageError("Cannot create a module loader for this printer package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = module
    try:
        spec.loader.exec_module(module)
        module_name, factory_name = manifest.entrypoint.split(":", 1)
        factory = getattr(importlib.import_module(f"{namespace}.{module_name}"), factory_name)
        plugin = factory() if callable(factory) else factory
        if not isinstance(plugin, PrinterPlugin):
            raise PluginPackageError("Package entrypoint must create a PrinterPlugin")
        if (plugin.id, plugin.version, plugin.api_version) != (
            manifest.plugin_id,
            manifest.version,
            manifest.api_version,
        ):
            raise PluginPackageError("Loaded printer plugin identity/version/API does not match its static manifest")
        if not plugin.__class__.__module__.startswith(namespace + "."):
            raise PluginPackageError("Package entrypoint must create its own version-isolated plugin implementation")
        plugin._installed_manifest = manifest
        plugin._package_namespace = namespace
        return plugin
    except BaseException:
        for name in tuple(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)
        raise

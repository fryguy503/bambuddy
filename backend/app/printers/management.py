"""Durable printer package selection, retained device versions and rollback.

Package uploads never execute code. Versions are immutable, and the selection
file is replaced atomically. A failed startup candidate can fall back only to
an earlier version of that same plugin, including the bundled Bambu provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.core.config import settings
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.packages import (
    PluginPackageError,
    inspect_package,
    load_package_plugin,
    safe_relative_path,
    validate_compatibility,
)
from backend.app.printers.sdk import PluginManifest

BUNDLED_PUBLISHERS = {"bambu": "bambuddy"}
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def printer_pin_identity(config: PrinterConfig) -> str:
    """Bind a saved printer ID to its provider and device, without credentials."""
    return hashlib.sha256(f"{config.plugin_id}\0{config.serial_number}".encode()).hexdigest()


class PluginManager:
    def __init__(self, root: Path | None = None):
        self.root = (
            Path(root) if root is not None else (settings.printer_plugin_dir or settings.base_dir / "printer-plugins")
        )

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def read_state(self) -> dict:
        if not self.state_path.exists():
            return {"format_version": 1, "plugins": {}, "trusted_keys": {}}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("format_version") != 1 or not isinstance(state.get("plugins"), dict):
                raise ValueError("Invalid state shape")
            if not isinstance(state.get("trusted_keys", {}), dict):
                raise ValueError("Invalid trust store")
            pins = state.get("printer_pins", {})
            if not isinstance(pins, dict):
                raise ValueError("Invalid printer version pins")
            for printer_id, pin in pins.items():
                if (
                    not isinstance(printer_id, str)
                    or not printer_id.isdecimal()
                    or int(printer_id) <= 0
                    or str(int(printer_id)) != printer_id
                    or not isinstance(pin, dict)
                    or set(pin) != {"plugin_id", "version", "identity"}
                    or not isinstance(pin.get("plugin_id"), str)
                    or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", pin["plugin_id"])
                    or not isinstance(pin.get("version"), str)
                    or not 1 <= len(pin["version"]) <= 128
                    or not isinstance(pin.get("identity"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", pin["identity"])
                ):
                    raise ValueError("Invalid saved printer version pin")
            return state
        except (OSError, ValueError, AttributeError) as exc:
            raise PluginPackageError(
                "Printer plugin selection state cannot be read; restore its last valid backup"
            ) from exc

    def get_printer_pins(self) -> dict[str, dict]:
        return {printer_id: dict(pin) for printer_id, pin in self.read_state().get("printer_pins", {}).items()}

    def set_printer_pin(self, config: PrinterConfig, version: str) -> None:
        if config.printer_id is None:
            return
        if (
            config.printer_id <= 0
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", config.plugin_id)
            or not isinstance(version, str)
            or not 1 <= len(version) <= 128
        ):
            raise PluginPackageError("Invalid printer version pin")
        with self.transaction() as state:
            state.setdefault("printer_pins", {})[str(config.printer_id)] = {
                "plugin_id": config.plugin_id,
                "version": version,
                "identity": printer_pin_identity(config),
            }

    def remove_printer_pin(self, printer_id: int, *, expected: dict | None = None) -> bool:
        if not self.state_path.exists():
            return False
        with self.transaction() as state:
            pins = state.get("printer_pins", {})
            current = pins.get(str(printer_id))
            if current is None or expected is not None and current != expected:
                return False
            del pins[str(printer_id)]
            return True

    def _write_state(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".state-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(state, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def transaction(self):
        """Use an OS lock, released on process failure, as well as a thread lock."""
        self.root.mkdir(parents=True, exist_ok=True)
        with _lock, (self.root / ".management.lock").open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PluginPackageError("Another printer plugin management operation is in progress") from exc
            try:
                state = self.read_state()
                yield state
                self._write_state(state)
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def version_path(self, plugin_id: str, version: str) -> Path:
        safe_relative_path(plugin_id)
        if "/" in plugin_id:
            raise PluginPackageError("Invalid printer plugin ID")
        key = hashlib.sha256(version.encode("utf-8")).hexdigest()[:24]
        path = self.root / "packages" / plugin_id / f"v-{key}"
        self._within_root(path)
        return path

    def _within_root(self, path: Path) -> None:
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise PluginPackageError("Plugin package path escaped its managed directory")

    def _remove_tree(self, path: Path) -> None:
        self._within_root(path)
        if path.exists():
            shutil.rmtree(path)

    def stage(
        self, content: bytes, *, expected_sha256: str | None = None, unmanaged_ids: set[str] | None = None
    ) -> dict:
        with self.transaction() as state:
            package = inspect_package(
                content, expected_sha256=expected_sha256, trusted_keys=state.get("trusted_keys", {})
            )
            manifest = package.manifest
            plugin_id, version = manifest.plugin_id, manifest.version
            bundled_manifest_path = Path(__file__).parent / plugin_id / "manifest.json"
            if plugin_id in BUNDLED_PUBLISHERS and bundled_manifest_path.is_file():
                bundled_manifest = PluginManifest.model_validate_json(bundled_manifest_path.read_text(encoding="utf-8"))
                if version == bundled_manifest.version and manifest.model_dump(
                    mode="json"
                ) != bundled_manifest.model_dump(mode="json"):
                    raise PluginPackageError(
                        "The bundled plugin version is immutable; changed packages require a new version"
                    )
            if plugin_id in (unmanaged_ids or set()):
                raise PluginPackageError(
                    "An unmanaged Python entry point owns this plugin ID; remove it explicitly first"
                )
            existing = state["plugins"].get(plugin_id)
            owner = existing.get("publisher_id") if existing else BUNDLED_PUBLISHERS.get(plugin_id)
            if owner is not None and owner != manifest.publisher_id:
                raise PluginPackageError("Plugin update publisher does not match the existing plugin owner")
            if (
                existing
                and any(item.get("signer_key_id") for item in existing.get("versions", {}).values())
                and not package.signer_key_id
            ):
                raise PluginPackageError("An update to a signed plugin also requires a trusted publisher signature")
            record = existing or {
                "publisher_id": manifest.publisher_id,
                "name": manifest.name,
                "enabled": plugin_id in BUNDLED_PUBLISHERS,
                "desired_version": None,
                "previous_version": None,
                "last_known_good": None,
                "versions": {},
                "last_error": None,
            }
            if record.get("remove_pending"):
                raise PluginPackageError("Plugin removal is pending; restart before installing another version")
            old = record["versions"].get(version)
            if old:
                if old["sha256"] != package.sha256:
                    raise PluginPackageError(
                        "A plugin version is immutable; publish a different version for changed contents"
                    )
                return self._staged_response(plugin_id, version, old)
            target = self.version_path(plugin_id, version)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self._within_root(target)
                # Recover an upload whose directory rename succeeded but whose
                # selection-file commit did not, without overwriting any bytes.
                try:
                    same_archive = hashlib.sha256((target / "package.zip").read_bytes()).hexdigest() == package.sha256
                    same_payload = all(
                        (target / "payload" / name).read_bytes() == data for name, data in package.files.items()
                    )
                except OSError:
                    same_archive = same_payload = False
                if not same_archive or not same_payload:
                    raise PluginPackageError("An unregistered package directory already exists with different contents")
            staging = target.parent / f".staging-{uuid.uuid4().hex}"
            try:
                (staging / "payload").mkdir(parents=True)
                for name, data in package.files.items():
                    relative = safe_relative_path(name)
                    destination = staging / "payload" / Path(*relative.parts)
                    self._within_root(destination)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                (staging / "package.zip").write_bytes(content)
                (staging / "manifest.json").write_text(
                    json.dumps(package.manifest_json, indent=2) + "\n", encoding="utf-8"
                )
                if target.exists():
                    self._remove_tree(staging)
                else:
                    os.replace(staging, target)
            except BaseException:
                self._remove_tree(staging)
                raise
            info = {
                "sha256": package.sha256,
                "installed_at": _now(),
                "source": "managed-package",
                "manifest": package.manifest_json,
                "signer_key_id": package.signer_key_id,
                "validation": package.validation,
            }
            record["versions"][version] = info
            state["plugins"][plugin_id] = record
            return self._staged_response(plugin_id, version, info)

    @staticmethod
    def _staged_response(plugin_id: str, version: str, info: dict) -> dict:
        return {
            "plugin_id": plugin_id,
            "version": version,
            "sha256": info["sha256"],
            "compatible": True,
            "validation": info["validation"],
            "activation": "staged",
            "restart_required": False,
        }

    def _verified_version(self, state: dict, plugin_id: str, version: str):
        record = state["plugins"].get(plugin_id, {})
        info = record.get("versions", {}).get(version)
        if info is None:
            raise PluginPackageError("Requested printer plugin version is not installed")
        directory = self.version_path(plugin_id, version)
        try:
            package = inspect_package(
                (directory / "package.zip").read_bytes(),
                expected_sha256=info["sha256"],
                trusted_keys=state.get("trusted_keys", {}),
            )
        except OSError as exc:
            raise PluginPackageError("Installed printer package archive is missing or unreadable") from exc
        if (package.manifest.plugin_id, package.manifest.version, package.manifest.publisher_id) != (
            plugin_id,
            version,
            record["publisher_id"],
        ):
            raise PluginPackageError("Stored printer package identity does not match its selection record")
        payload = directory / "payload"
        actual = set()
        for path in payload.rglob("*"):
            self._within_root(path)
            if path.is_symlink():
                raise PluginPackageError("Installed printer package contains a symbolic link")
            if path.is_file():
                relative = path.relative_to(payload).as_posix()
                if "__pycache__" in path.relative_to(payload).parts and path.suffix == ".pyc":
                    path.unlink()  # Never execute cached bytecode instead of the verified source.
                    continue
                actual.add(relative)
                expected = package.files.get(relative)
                if expected is None or path.read_bytes() != expected:
                    raise PluginPackageError("Installed printer package payload failed integrity validation")
        if actual != set(package.files):
            raise PluginPackageError("Installed printer package payload inventory does not match")
        return package, payload

    def load_version(self, plugin_id: str, version: str):
        state = self.read_state()
        package, payload = self._verified_version(state, plugin_id, version)
        return load_package_plugin(payload, package.manifest, package.sha256), package.manifest

    def selection_manifest(self, plugin_id: str, version: str) -> PluginManifest:
        """Read and validate the selected contract without importing its code."""
        if version == "bundled":
            if plugin_id not in BUNDLED_PUBLISHERS:
                raise PluginPackageError("This printer plugin has no bundled version")
            path = Path(__file__).parent / plugin_id / "manifest.json"
            try:
                manifest = PluginManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PluginPackageError("The bundled printer manifest cannot be read") from exc
            validate_compatibility(manifest)
            return manifest
        package, _ = self._verified_version(self.read_state(), plugin_id, version)
        return package.manifest

    def rollback_target(self, plugin_id: str, version: str | None = None) -> str:
        record = self.read_state()["plugins"].get(plugin_id, {})
        target = version or record.get("previous_version")
        if target is None and plugin_id in BUNDLED_PUBLISHERS:
            target = "bundled"
        if target is None:
            raise PluginPackageError("No previous printer plugin version is available")
        return target

    def selection_checkpoint(self, plugin_id: str) -> dict:
        record = self.read_state()["plugins"].get(plugin_id, {})
        return {
            key: record[key]
            for key in (
                "desired_version",
                "enabled",
                "previous_version",
                "last_known_good",
                "loaded_version",
                "last_error",
                "selected_at",
            )
            if key in record
        }

    def restore_selection(self, plugin_id: str, checkpoint: dict, *, expected_version: str) -> None:
        with self.transaction() as state:
            record = state["plugins"].get(plugin_id)
            if record is None or record.get("desired_version") != expected_version:
                raise PluginPackageError("Plugin selection changed while activation was being restored")
            for key in (
                "desired_version",
                "enabled",
                "previous_version",
                "last_known_good",
                "loaded_version",
                "last_error",
                "selected_at",
            ):
                record.pop(key, None)
            record.update(checkpoint)

    def select(self, plugin_id: str, version: str, *, loaded_version: str | None = None, live: bool = False) -> dict:
        with self.transaction() as state:
            record = state["plugins"].get(plugin_id)
            if record is None:
                if plugin_id not in BUNDLED_PUBLISHERS or version != "bundled":
                    raise PluginPackageError("Printer plugin is not installed")
                record = {"publisher_id": BUNDLED_PUBLISHERS[plugin_id], "name": plugin_id, "versions": {}}
                state["plugins"][plugin_id] = record
            if version == "bundled":
                if plugin_id not in BUNDLED_PUBLISHERS:
                    raise PluginPackageError("This printer plugin has no bundled version")
            else:
                self._verified_version(state, plugin_id, version)
            previous = (
                loaded_version
                or record.get("last_known_good")
                or ("bundled" if plugin_id in BUNDLED_PUBLISHERS else None)
            )
            if previous != version:
                record["previous_version"] = previous
            record.update(desired_version=version, enabled=True, last_error=None, selected_at=_now())
            return {
                "plugin_id": plugin_id,
                "desired_version": version,
                "restart_required": not live and loaded_version != version,
            }

    def rollback(self, plugin_id: str, *, version: str | None = None, loaded_version: str | None = None) -> dict:
        target = self.rollback_target(plugin_id, version)
        return self.select(plugin_id, target, loaded_version=loaded_version)

    def set_enabled(self, plugin_id: str, enabled: bool, *, loaded: bool) -> dict:
        with self.transaction() as state:
            if not enabled and any(pin["plugin_id"] == plugin_id for pin in state.get("printer_pins", {}).values()):
                raise PluginPackageError("A retained printer still uses this plugin")
            if plugin_id not in state["plugins"]:
                if plugin_id not in BUNDLED_PUBLISHERS:
                    raise PluginPackageError("Printer plugin is not installed")
                state["plugins"][plugin_id] = {
                    "publisher_id": BUNDLED_PUBLISHERS[plugin_id],
                    "name": plugin_id,
                    "enabled": True,
                    "desired_version": "bundled",
                    "versions": {},
                }
            if (
                enabled
                and not state["plugins"][plugin_id].get("desired_version")
                and plugin_id not in BUNDLED_PUBLISHERS
            ):
                raise PluginPackageError("Select an installed plugin version before enabling it")
            state["plugins"][plugin_id]["enabled"] = enabled
            return {"plugin_id": plugin_id, "enabled": enabled, "restart_required": enabled != loaded}

    def remove_version(
        self,
        plugin_id: str,
        version: str,
        *,
        loaded_version: str | None = None,
        loaded_versions: set[str] | None = None,
    ) -> dict:
        with self.transaction() as state:
            record = state["plugins"].get(plugin_id, {})
            if version not in record.get("versions", {}):
                raise PluginPackageError("Printer plugin version is not installed")
            protected = {
                loaded_version,
                record.get("desired_version"),
                record.get("last_known_good"),
                record.get("previous_version"),
            }
            protected.update(loaded_versions or ())
            protected.update(
                pin["version"] for pin in state.get("printer_pins", {}).values() if pin["plugin_id"] == plugin_id
            )
            if version in protected:
                raise PluginPackageError("Selected, loaded and rollback versions cannot be removed")
            self._remove_tree(self.version_path(plugin_id, version))
            del record["versions"][version]
            return {"plugin_id": plugin_id, "version": version, "removed": True, "restart_required": False}

    def remove_plugin(self, plugin_id: str) -> dict:
        if plugin_id in BUNDLED_PUBLISHERS:
            raise PluginPackageError("Bundled printer support can be disabled or rolled back, but cannot be deleted")
        with self.transaction() as state:
            record = state["plugins"].get(plugin_id)
            if record is None:
                raise PluginPackageError("Printer plugin is not installed")
            if any(pin["plugin_id"] == plugin_id for pin in state.get("printer_pins", {}).values()):
                raise PluginPackageError("A retained printer still uses this plugin")
            record.update(enabled=False, remove_pending=True, desired_version=None)
            return {"plugin_id": plugin_id, "removed": False, "remove_pending": True, "restart_required": True}

    def finalize_removals(self) -> None:
        if not self.state_path.exists():
            return
        if not any(record.get("remove_pending") for record in self.read_state()["plugins"].values()):
            return
        with self.transaction() as state:
            for plugin_id, record in list(state["plugins"].items()):
                if record.get("remove_pending"):
                    if any(pin["plugin_id"] == plugin_id for pin in state.get("printer_pins", {}).values()):
                        record["last_error"] = "Plugin removal waits for retained printer versions to be released"
                        continue
                    self._remove_tree(self.root / "packages" / plugin_id)
                    del state["plugins"][plugin_id]

    def record_load(self, plugin_id: str, version: str | None, *, error: str | None = None) -> None:
        if plugin_id not in self.read_state()["plugins"]:
            return
        with self.transaction() as state:
            record = state["plugins"][plugin_id]
            record["loaded_version"] = version
            record["last_error"] = error
            if version is not None and error is None:
                record["last_known_good"] = version

    def trust_key(self, key_id: str, publisher_id: str, public_key: str) -> dict:
        safe_relative_path(key_id)
        if "/" in key_id or len(key_id) > 128 or not publisher_id or len(publisher_id) > 128:
            raise PluginPackageError("Invalid publisher key identity")
        try:
            raw = base64.b64decode(public_key, validate=True)
        except (ValueError, TypeError) as exc:
            raise PluginPackageError("Publisher public key must be base64") from exc
        if len(raw) != 32:
            raise PluginPackageError("Ed25519 public keys must contain 32 bytes")
        with self.transaction() as state:
            existing = state.setdefault("trusted_keys", {}).get(key_id)
            key = {"publisher_id": publisher_id, "public_key": public_key}
            if existing is not None and any(existing.get(name) != value for name, value in key.items()):
                raise PluginPackageError("Trusted key IDs cannot be reassigned; add a new key ID")
            state["trusted_keys"][key_id] = key
        return {"key_id": key_id, "publisher_id": publisher_id, "algorithm": "ed25519"}

    def revoke_key(self, key_id: str) -> dict:
        with self.transaction() as state:
            if key_id not in state.setdefault("trusted_keys", {}):
                raise PluginPackageError("Publisher key is not trusted")
            state["trusted_keys"][key_id]["revoked"] = True
        return {"key_id": key_id, "revoked": True, "restart_required": True}

    def describe(self, registry: Any, configured_counts: dict[str, int] | None = None) -> dict:
        from backend.app.printers.lifecycle import lifecycle
        from backend.app.printers.rolling import rollouts

        registry.load()
        state = self.read_state()
        configured_counts = configured_counts or {}
        plugin_ids = set(state["plugins"]) | set(registry._plugins)
        items = []
        for plugin_id in sorted(plugin_ids):
            record = state["plugins"].get(plugin_id, {})
            plugin = registry._plugins.get(plugin_id)
            loaded = registry.loaded_sources.get(plugin_id, {})
            loaded_version = loaded.get("version")
            desired = record.get("desired_version") or ("bundled" if plugin_id in BUNDLED_PUBLISHERS else None)
            enabled = record.get("enabled", True)
            effective = "bundled" if loaded.get("source") == "bundled" else loaded_version
            running = registry.running_versions(plugin_id)
            active_keys = {item.get("version_key", item["version"]) for item in running}
            for service_key in lifecycle.protected_versions(plugin_id) - active_keys:
                owner = registry.prepare(plugin_id, service_key)
                running.append(
                    {
                        "version": owner.plugin.version,
                        "version_key": owner.version,
                        "source": owner.source["source"],
                        "printer_ids": [],
                        "service_owner": True,
                    }
                )
            versions = []
            for version, info in record.get("versions", {}).items():
                compatibility_error = None
                try:
                    validate_compatibility(PluginManifest.model_validate(info["manifest"]))
                except ValueError as exc:
                    compatibility_error = str(exc)
                validation = dict(info.get("validation", {}))
                signer = info.get("signer_key_id")
                if signer and (
                    signer not in state.get("trusted_keys", {}) or state["trusted_keys"][signer].get("revoked")
                ):
                    compatibility_error = "Publisher signing key is no longer trusted"
                    validation.update(signature_verified=False, trust="signature-key-revoked")
                versions.append(
                    {
                        "version": version,
                        "sha256": info["sha256"],
                        "installed_at": info["installed_at"],
                        "source": info["source"],
                        "compatible": compatibility_error is None,
                        "validation": validation,
                        "compatibility_error": compatibility_error,
                    }
                )
            items.append(
                {
                    "id": plugin_id,
                    "name": plugin.name if plugin else record.get("name", plugin_id),
                    "loaded_version": loaded_version,
                    "running_versions": running,
                    "rollout": rollouts.describe(plugin_id),
                    "service_error": lifecycle.errors.get(plugin_id),
                    "bundled_version": registry.bundled_versions.get(plugin_id),
                    "enabled": enabled,
                    "effective_enabled": plugin is not None,
                    "desired_version": desired,
                    "previous_version": record.get("previous_version"),
                    "last_known_good": record.get("last_known_good"),
                    "remove_pending": record.get("remove_pending", False),
                    "restart_required": bool(
                        record.get("remove_pending")
                        or (enabled != (plugin is not None))
                        or (enabled and desired != effective)
                    ),
                    "versions": versions,
                    "load_error": registry.load_errors.get(plugin_id) or record.get("last_error"),
                    "configured_printers": configured_counts.get(plugin_id, 0),
                    "publisher_id": record.get("publisher_id", BUNDLED_PUBLISHERS.get(plugin_id)),
                    "source": loaded.get("source"),
                    "host_managed": loaded.get("source") == "entry-point",
                }
            )
        return {
            "plugins": items,
            "load_errors": registry.load_errors,
            "trusted_keys": [
                {
                    "key_id": key_id,
                    "publisher_id": item["publisher_id"],
                    "algorithm": "ed25519",
                    "revoked": item.get("revoked", False),
                }
                for key_id, item in state.get("trusted_keys", {}).items()
            ],
            "policy": {
                "restart_required_on_changes": False,
                "activation": "rolling-per-printer",
                "restart_required_on_disable_or_remove": True,
                "trust_mode": "administrator-installed-code",
                "automatic_dependency_installation": False,
                "hardware_certification": False,
            },
        }

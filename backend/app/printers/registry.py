"""Printer plugin registration and installed addon discovery."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.metadata import entry_points
from threading import RLock
from typing import TYPE_CHECKING

from backend.app.printers.contracts import (
    PrinterConfig,
    PrinterPlugin,
    PrinterPluginError,
    UnknownPrinterPlugin,
)

logger = logging.getLogger(__name__)
if TYPE_CHECKING:
    from backend.app.printers.sdk import PluginManifest


@dataclass(frozen=True)
class RuntimeProvider:
    plugin: PrinterPlugin
    version: str
    source: dict
    manifest: PluginManifest | None = None


_loading_registry: ContextVar[PrinterPluginRegistry | None] = ContextVar(
    "printer_plugin_loading_registry", default=None
)
_provider_context: ContextVar[tuple[int, PrinterPlugin] | None] = ContextVar("printer_provider_context", default=None)
_service_context: ContextVar[PrinterPlugin | None] = ContextVar("printer_service_provider_context", default=None)


def context_provider(printer_id: int | None) -> PrinterPlugin | None:
    binding = _provider_context.get()
    return binding[1] if binding is not None and binding[0] == printer_id else None


def context_provider_for_plugin(plugin_id: str) -> PrinterPlugin | None:
    binding = _provider_context.get()
    if binding is not None and binding[1].id == plugin_id:
        return binding[1]
    service = _service_context.get()
    return service if service is not None and service.id == plugin_id else None


@contextmanager
def provider_service_context(provider: PrinterPlugin):
    token = _service_context.set(provider)
    try:
        yield
    finally:
        _service_context.reset(token)


@contextmanager
def provider_context(printer: object, provider: PrinterPlugin):
    config = PrinterConfig.from_printer(printer)
    if config.printer_id is None or config.plugin_id != provider.id:
        raise PrinterPluginError("Workflow provider context does not match its printer")
    token = _provider_context.set((config.printer_id, provider))
    try:
        yield
    finally:
        _provider_context.reset(token)


def current_registry() -> PrinterPluginRegistry:
    """Use the registry selecting a package while its imports execute."""
    return _loading_registry.get() or registry


class PrinterPluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, PrinterPlugin] = {}
        self._loaded = False
        self.load_errors: dict[str, str] = {}
        self.loaded_sources: dict[str, dict] = {}
        self.bundled_versions: dict[str, str] = {}
        self._runtime: dict[tuple[str, str], RuntimeProvider] = {}
        self._pins: dict[int, tuple[str, RuntimeProvider]] = {}
        self._runtime_lock = RLock()
        self._runtime_refs: dict[str, tuple[RuntimeProvider, int | None, str]] = {}

    def _validate(self, plugin: PrinterPlugin, manifest=None):
        if not isinstance(plugin, PrinterPlugin):
            raise PrinterPluginError("Printer addon must implement PrinterPlugin")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", plugin.id):
            raise PrinterPluginError("Printer addon id must be a lowercase identifier")
        if plugin.api_version not in {1, 2}:
            raise PrinterPluginError(f"Unsupported printer addon API version: {plugin.api_version}")
        if len(plugin.operations) != len(set(plugin.operations)):
            raise PrinterPluginError("Printer addon commands must have unique names")
        if len(plugin.models) != len({model.id for model in plugin.models}):
            raise PrinterPluginError("Printer addon models must have unique ids")
        from backend.app.core.config import APP_VERSION
        from backend.app.core.permissions import Permission
        from backend.app.printers.sdk import validate_plugin_contract

        validated_manifest = validate_plugin_contract(
            plugin,
            manifest,
            core_version=APP_VERSION,
            known_permissions={permission.value for permission in Permission},
        )
        from backend.app.printers.packages import validate_compatibility

        validate_compatibility(validated_manifest)
        return validated_manifest

    def register(self, plugin: PrinterPlugin, *, manifest=None, source: str = "entry-point") -> None:
        if plugin.id in self._plugins:
            raise PrinterPluginError(f"Printer addon {plugin.id!r} is already registered")
        validated_manifest = self._validate(plugin, manifest)
        self._plugins[plugin.id] = plugin
        self.loaded_sources[plugin.id] = {
            "version": plugin.version,
            "source": source,
            "module_namespace": getattr(plugin, "_package_namespace", None)
            or plugin.__class__.__module__.rsplit(".", 1)[0],
        }
        version = "bundled" if source == "bundled" else plugin.version
        self._runtime[(plugin.id, version)] = RuntimeProvider(
            plugin, version, dict(self.loaded_sources[plugin.id]), validated_manifest
        )

    def prepare(self, plugin_id: str, version: str) -> RuntimeProvider:
        """Verify/import one immutable version without replacing its running peers."""
        self.load()
        with self._runtime_lock:
            existing = self._runtime.get((plugin_id, version))
            if existing is not None:
                return existing
            from backend.app.printers.management import PluginManager

            token = _loading_registry.set(self)
            try:
                if version == "bundled":
                    if plugin_id != "bambu":
                        raise UnknownPrinterPlugin(f"Printer addon {plugin_id!r} has no bundled version")
                    from backend.app.printers.bambu.plugin import BambuPrinterPlugin

                    plugin = BambuPrinterPlugin()
                    manifest = PluginManager().selection_manifest(plugin_id, version)
                    source_kind = "bundled"
                else:
                    plugin, manifest = PluginManager().load_version(plugin_id, version)
                    source_kind = "managed-package"
                validated = self._validate(plugin, manifest)
            finally:
                _loading_registry.reset(token)
            source = {
                "version": plugin.version,
                "source": source_kind,
                "module_namespace": getattr(plugin, "_package_namespace", None)
                or plugin.__class__.__module__.rsplit(".", 1)[0],
            }
            candidate = RuntimeProvider(plugin, version, source, validated)
            self._runtime[(plugin_id, version)] = candidate
            return candidate

    def activate(self, candidate: RuntimeProvider) -> None:
        """Publish only the default. Existing printers retain their pinned instance."""
        with self._runtime_lock:
            self._runtime[(candidate.plugin.id, candidate.version)] = candidate
            self._plugins[candidate.plugin.id] = candidate.plugin
            self.loaded_sources[candidate.plugin.id] = dict(candidate.source)
            self.load_errors.pop(candidate.plugin.id, None)

    def provider_for(self, printer: object) -> PrinterPlugin:
        config = PrinterConfig.from_printer(printer)
        self.load()
        workflow = context_provider(config.printer_id)
        if workflow is not None and workflow.id == config.plugin_id:
            return workflow
        if config.printer_id is not None:
            from backend.app.printers.management import PluginManager, printer_pin_identity

            identity = printer_pin_identity(config)
            with self._runtime_lock:
                pin = self._pins.get(config.printer_id)
                if pin is not None and pin[0] == identity and pin[1].plugin.id == config.plugin_id:
                    return pin[1].plugin
            saved = PluginManager().get_printer_pins().get(str(config.printer_id))
            if saved and saved["identity"] == identity and saved["plugin_id"] == config.plugin_id:
                candidate = self.prepare(config.plugin_id, saved["version"])
                with self._runtime_lock:
                    self._pins[config.printer_id] = (identity, candidate)
                return candidate.plugin
        return self.get(config.plugin_id)

    def pin_printer(self, printer: object, plugin: PrinterPlugin) -> None:
        config = PrinterConfig.from_printer(printer)
        if config.printer_id is None:
            return
        if config.plugin_id != plugin.id:
            raise PrinterPluginError("Printer and provider identity do not match")
        from backend.app.printers.management import PluginManager, printer_pin_identity

        with self._runtime_lock:
            candidate = next((item for item in self._runtime.values() if item.plugin is plugin), None)
            if candidate is None:
                raise PrinterPluginError("Cannot pin an unregistered printer provider instance")
            PluginManager().set_printer_pin(config, candidate.version)
            self._pins[config.printer_id] = (printer_pin_identity(config), candidate)

    def release_printer(self, printer_id: int, *, forget: bool = False, expected_pin: dict | None = None) -> None:
        from backend.app.printers.management import PluginManager

        with self._runtime_lock:
            if forget:
                removed = PluginManager().remove_printer_pin(printer_id, expected=expected_pin)
                if expected_pin is not None and not removed:
                    return
            self._pins.pop(printer_id, None)

    def running_versions(self, plugin_id: str) -> list[dict]:
        self.load()
        with self._runtime_lock:
            active: dict[str, dict] = {}
            default = self._plugins.get(plugin_id)
            for candidate in self._runtime.values():
                if candidate.plugin.id != plugin_id:
                    continue
                refs = [ref for ref in self._runtime_refs.values() if ref[0] is candidate]
                printer_ids = sorted(
                    {key for key, (_, pin) in self._pins.items() if pin is candidate}
                    | {printer_id for _, printer_id, _ in refs if printer_id is not None}
                )
                if candidate.plugin is default or printer_ids or refs:
                    active[candidate.version] = {
                        "version": candidate.plugin.version,
                        "version_key": candidate.version,
                        "source": candidate.source["source"],
                        "printer_ids": printer_ids,
                        "reference_count": len(refs),
                        "reasons": sorted({reason for _, _, reason in refs}),
                    }
            return list(active.values())

    def runtime_plugins(self) -> tuple[PrinterPlugin, ...]:
        self.load()
        return tuple(item.plugin for item in self._runtime.values())

    def runtime_for(self, plugin: PrinterPlugin) -> RuntimeProvider:
        self.load()
        with self._runtime_lock:
            for candidate in self._runtime.values():
                if candidate.plugin is plugin:
                    return candidate
        raise PrinterPluginError("Provider instance is not registered")

    def retain_runtime(
        self, plugin: PrinterPlugin, key: str, printer_id: int | None = None, reason: str = "operation"
    ) -> None:
        with self._runtime_lock:
            self._runtime_refs[key] = (self.runtime_for(plugin), printer_id, reason)

    def release_runtime(self, key: str) -> None:
        with self._runtime_lock:
            self._runtime_refs.pop(key, None)

    def protected_runtime_versions(self, plugin_id: str) -> set[str]:
        return {item["version_key"] for item in self.running_versions(plugin_id)}

    def load(self) -> None:
        if self._loaded:
            return
        token = _loading_registry.set(self)
        try:
            self._load_plugins()
        finally:
            _loading_registry.reset(token)

    def _load_plugins(self) -> None:
        from backend.app.printers.bambu.plugin import BambuPrinterPlugin
        from backend.app.printers.management import PluginManager

        self._loaded = True
        bundled = {"bambu": BambuPrinterPlugin()}
        self.bundled_versions = {plugin_id: plugin.version for plugin_id, plugin in bundled.items()}
        manager = PluginManager()
        try:
            manager.finalize_removals()
            state = manager.read_state()
        except Exception:
            self._loaded = False
            raise
        for plugin_id in sorted(set(bundled) | set(state["plugins"])):
            record = state["plugins"].get(plugin_id, {})
            if not record.get("enabled", True):
                continue
            desired = record.get("desired_version") or ("bundled" if plugin_id in bundled else None)
            candidates = [desired, record.get("last_known_good"), record.get("previous_version")]
            if plugin_id in bundled:
                candidates.append("bundled")
            failures = []
            visited = set()
            for version in candidates:
                if version is None or version in visited:
                    continue
                visited.add(version)
                try:
                    if version == "bundled":
                        self.register(bundled[plugin_id], source="bundled")
                    else:
                        plugin, manifest = manager.load_version(plugin_id, version)
                        self.register(plugin, manifest=manifest, source="managed-package")
                    error = "; ".join(failures) or None
                    if error:
                        self.load_errors[plugin_id] = error
                    try:
                        manager.record_load(plugin_id, version, error=error)
                    except (OSError, PrinterPluginError) as exc:
                        self.load_errors[plugin_id] = f"Loaded {version}, but could not persist load status: {exc}"
                        logger.exception("Unable to persist printer addon %s load status", plugin_id)
                    break
                except Exception as exc:
                    failures.append(f"{version}: {type(exc).__name__}: {exc}")
                    logger.exception("Unable to load printer addon %s version %s", plugin_id, version)
            else:
                if failures:
                    self.load_errors[plugin_id] = "; ".join(failures)
                    try:
                        manager.record_load(plugin_id, None, error=self.load_errors[plugin_id])
                    except (OSError, PrinterPluginError):
                        logger.exception("Unable to persist printer addon %s load failure", plugin_id)
        for entry in sorted(entry_points(group="bambuddy.printers"), key=lambda item: item.name):
            try:
                addon = entry.load()
                plugin = addon() if callable(addon) else addon
                if not isinstance(plugin, PrinterPlugin):
                    raise PrinterPluginError("Printer addon must implement PrinterPlugin")
                if not state["plugins"].get(plugin.id, {}).get("enabled", True):
                    continue
                self.register(plugin)
            except Exception as exc:
                self.load_errors[entry.name] = str(exc)
                logger.exception("Unable to load printer addon %s", entry.name)
        # A process restart must not silently move a busy printer onto the new
        # default. Restore the exact immutable instances selected for its last
        # connection; provider_for checks the saved hardware identity again.
        for printer_id, saved in state.get("printer_pins", {}).items():
            try:
                candidate = self.prepare(saved["plugin_id"], saved["version"])
                self._pins[int(printer_id)] = (saved["identity"], candidate)
            except Exception as exc:
                self.load_errors[f"{saved['plugin_id']}:printer:{printer_id}"] = str(exc)
                logger.exception("Unable to restore provider pin for printer %s", printer_id)

    def get(self, plugin_id: str = "bambu") -> PrinterPlugin:
        self.load()
        try:
            return self._plugins[plugin_id]
        except KeyError as exc:
            raise UnknownPrinterPlugin(f"Printer addon {plugin_id!r} is not installed") from exc

    def all(self) -> tuple[PrinterPlugin, ...]:
        self.load()
        return tuple(self._plugins.values())

    def descriptors(self) -> list[dict]:
        return [plugin.descriptor() for plugin in self.all()]


registry = PrinterPluginRegistry()


def get_plugin(plugin_id: str = "bambu") -> PrinterPlugin:
    return registry.get(plugin_id)


def plugin_for(printer: object) -> PrinterPlugin:
    return registry.provider_for(printer)


def register_plugin(plugin: PrinterPlugin) -> None:
    registry.load()
    registry.register(plugin)


def list_plugins() -> tuple[PrinterPlugin, ...]:
    return registry.all()


def descriptors() -> list[dict]:
    return registry.descriptors()

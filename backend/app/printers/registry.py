"""Printer plugin registration and installed addon discovery."""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from importlib.metadata import entry_points

from backend.app.printers.contracts import (
    PrinterConfig,
    PrinterPlugin,
    PrinterPluginError,
    UnknownPrinterPlugin,
)

logger = logging.getLogger(__name__)
_loading_registry: ContextVar[PrinterPluginRegistry | None] = ContextVar(
    "printer_plugin_loading_registry", default=None
)


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

    def register(self, plugin: PrinterPlugin, *, manifest=None, source: str = "entry-point") -> None:
        if not isinstance(plugin, PrinterPlugin):
            raise PrinterPluginError("Printer addon must implement PrinterPlugin")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", plugin.id):
            raise PrinterPluginError("Printer addon id must be a lowercase identifier")
        if plugin.api_version not in {1, 2}:
            raise PrinterPluginError(f"Unsupported printer addon API version: {plugin.api_version}")
        if plugin.id in self._plugins:
            raise PrinterPluginError(f"Printer addon {plugin.id!r} is already registered")
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
        self._plugins[plugin.id] = plugin
        self.loaded_sources[plugin.id] = {
            "version": plugin.version,
            "source": source,
            "module_namespace": getattr(plugin, "_package_namespace", None)
            or plugin.__class__.__module__.rsplit(".", 1)[0],
        }

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
    return get_plugin(PrinterConfig.from_printer(printer).plugin_id)


def register_plugin(plugin: PrinterPlugin) -> None:
    registry.load()
    registry.register(plugin)


def list_plugins() -> tuple[PrinterPlugin, ...]:
    return registry.all()


def descriptors() -> list[dict]:
    return registry.descriptors()

"""Resolve deprecated vendor imports against the selected plugin package.

Only compatibility callers use this module. The generic API depends on SDK
descriptors and operations, not these private vendor module names.
"""

from importlib import import_module


def compatibility_module(plugin_id: str, module: str):
    from backend.app.printers.registry import context_provider_for_plugin, current_registry

    registry = current_registry()
    registry.load()
    provider = context_provider_for_plugin(plugin_id)
    source = registry.runtime_for(provider).source if provider is not None else registry.loaded_sources.get(plugin_id)
    if source is None:
        # Deprecated code/type imports remain importable when the included
        # provider is disabled. This does not register or connect a provider;
        # every managed device still resolves through registry.get().
        if plugin_id == "bambu":
            return import_module(f"backend.app.printers.bambu.{module}")
        registry.get(plugin_id)
        raise ImportError(f"Plugin {plugin_id!r} has no selected package")
    return import_module(f"{source['module_namespace']}.{module}")

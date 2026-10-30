"""A real bundled Bambu payload can be updated without changing core code."""

import hashlib
import io
import json
import zipfile
from importlib import import_module
from pathlib import Path

import pytest

from backend.app.core.config import APP_VERSION
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.management import PluginManager
from backend.app.printers.registry import PrinterPluginRegistry


@pytest.mark.asyncio
async def test_bambu_package_updates_protocol_models_and_version_independently(tmp_path, monkeypatch):
    from backend.app.printers import registry as registry_module
    from backend.app.printers.bambu.plugin import BambuPrinterPlugin

    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    original = BambuPrinterPlugin()
    baseline_core_version = APP_VERSION
    source = Path(__file__).resolve().parents[2] / "app" / "printers" / "bambu"
    files = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name != "manifest.json"
        and path.suffix not in {".pyc", ".pyo"}
    }
    plugin_source = files["plugin.py"].decode("utf-8")
    assert 'version = "1.0.0"' in plugin_source
    plugin_source = plugin_source.replace('version = "1.0.0"', 'version = "1.0.1"', 1)
    plugin_source += (
        "\nBambuPrinterPlugin.models = (*BambuPrinterPlugin.models, "
        'PrinterModel("FUTURE", "Future Bambu", capabilities=_BASE))\n'
    )
    files["plugin.py"] = plugin_source.encode("utf-8")
    files["streams.py"] += (
        b"\nasync def generate_chamber_mjpeg_stream(*args, **kwargs):\n"
        b"    yield b'updated-chamber-stream'\n"
        b"\nasync def generate_rtsp_mjpeg_stream(*args, **kwargs):\n"
        b"    yield b'updated-rtsp-stream'\n"
    )
    files["discovery.py"] += b"\nBAMBU_SEARCH_TARGET = 'updated-discovery-target'\n"
    files["materials.py"] += b"\ndef global_tray_id(ams_id, tray_id):\n    return 1000 + ams_id + tray_id\n"
    files["ams_humidity.py"] += b"\ndef ams_humidity_percent(unit):\n    return 17.5\n"
    files["ams_drying.py"] += b"\ndef is_drying_active(unit):\n    return True\n"
    files["fts_routing.py"] += b"\ndef extruder_for_inlet(inlet):\n    return 7\n"
    files["kprofile_lookup.py"] += b"\ndef build_slot_k_resolver(state):\n    return lambda *args: 0.123\n"
    files["slot_nozzle.py"] += (
        b"\ndef resolve_slot_nozzle(*args, **kwargs):\n    return SlotNozzle(extruder=7, diameter='0.8')\n"
    )
    manifest = original.sdk_manifest().model_dump(mode="json")
    manifest.update(version="1.0.1", entrypoint="plugin:BambuPrinterPlugin")
    manifest["models"].append({"id": "FUTURE", "name": "Future Bambu", "capabilities": sorted(original.capabilities)})
    manifest["files"] = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("manifest.json", json.dumps(manifest))
        for name, content in files.items():
            output.writestr("payload/" + name, content)
    manager = PluginManager()
    staged = manager.stage(archive.getvalue())
    assert staged["version"] == "1.0.1"
    manager.select("bambu", "1.0.1", loaded_version="bundled")
    restarted = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", restarted)
    updated = restarted.get("bambu")
    assert updated.version == "1.0.1", restarted.load_errors
    assert updated.model("FUTURE").name == "Future Bambu"
    assert original.version == "1.0.0" and original.model("FUTURE") is None
    assert baseline_core_version == APP_VERSION
    namespace = restarted.loaded_sources["bambu"]["module_namespace"]
    assert namespace.startswith("bambuddy_printer_plugins.bambu_")
    connection = updated.create_connection(PrinterConfig("192.0.2.50", "BAMBUTEST", "12345678"))
    assert connection.__class__.__module__ == namespace + ".mqtt"
    assert updated.virtual_printer_service().__class__.__module__ == namespace + ".virtual_printer.manager"
    assert all(
        route.endpoint.__module__.startswith(namespace)
        for router in updated.legacy_http_routers()
        for route in router.routes
    )
    from backend.app.api.routes import camera
    from backend.app.services import discovery

    assert [chunk async for chunk in camera.generate_chamber_mjpeg_stream("192.0.2.50", "12345678", "FUTURE")] == [
        b"updated-chamber-stream"
    ]
    assert [chunk async for chunk in camera.generate_rtsp_mjpeg_stream("192.0.2.50", "12345678", "FUTURE")] == [
        b"updated-rtsp-stream"
    ]
    assert discovery.BAMBU_SEARCH_TARGET == "updated-discovery-target"
    assert discovery.PrinterDiscoveryService.__module__ == namespace + ".discovery"
    assert discovery.is_running_in_docker.__module__ == "backend.app.services.discovery"
    from backend.app.printers.compat import compatibility_module

    cloud_routes = compatibility_module("bambu", "cloud_routes")
    assert Path(cloud_routes.__file__).parent / "data" == cloud_routes.FIELDS_DATA_DIR
    for preset_type in ("filament", "process", "printer"):
        assert cloud_routes._load_fields(preset_type)
    from backend.app.services.print_scheduler import _global_tray_id

    assert _global_tray_id(0, 0) == 1000

    # A real application restart imports compatibility paths after selecting
    # its package. Clear only these cached aliases to reproduce that boundary.
    import sys

    checks = {
        "backend.app.utils.ams_humidity": ("ams_humidity", lambda module: module.ams_humidity_percent({}) == 17.5),
        "backend.app.utils.ams_drying": ("ams_drying", lambda module: module.is_drying_active({}) is True),
        "backend.app.utils.fts_routing": ("fts_routing", lambda module: module.extruder_for_inlet("A") == 7),
        "backend.app.utils.kprofile_lookup": (
            "kprofile_lookup",
            lambda module: module.build_slot_k_resolver(None)(None, 0, 0) == 0.123,
        ),
        "backend.app.services.slot_nozzle": (
            "slot_nozzle",
            lambda module: module.resolve_slot_nozzle(None, 0, 0).diameter == "0.8",
        ),
    }
    for historical_path, (leaf, check) in checks.items():
        parent_path, _, attribute = historical_path.rpartition(".")
        parent = import_module(parent_path)
        monkeypatch.setattr(parent, attribute, getattr(parent, attribute, None), raising=False)
        # Track absent aliases too, so fixture imports cannot leak a selected
        # managed implementation into tests using the restored registry.
        monkeypatch.setitem(sys.modules, historical_path, sys.modules.get(historical_path))
        sys.modules.pop(historical_path)
        compatibility = import_module(historical_path)
        assert compatibility is import_module(f"{namespace}.{leaf}")
        assert check(compatibility)

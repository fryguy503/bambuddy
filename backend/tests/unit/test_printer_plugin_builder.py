"""The documented release command builds the selected source reproducibly."""

import shutil
from pathlib import Path

from backend.app.printers.packages import inspect_package
from scripts.build_printer_plugin import build


def test_builder_uses_release_source_and_produces_installable_identical_archives(tmp_path):
    included = Path(__file__).resolve().parents[2] / "app/printers/bambu"
    source = tmp_path / "bambu-release"
    shutil.copytree(included, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    factory = source / "plugin.py"
    factory.write_text(
        factory.read_text(encoding="utf-8").replace('version = "1.0.0"', 'version = "1.0.1"', 1), encoding="utf-8"
    )
    first, retry = tmp_path / "first.bbp", tmp_path / "retry.bbp"
    manifest = build(source, first)
    build(source, retry)
    assert manifest.version == "1.0.1"
    assert first.read_bytes() == retry.read_bytes()
    inspected = inspect_package(first.read_bytes())
    assert inspected.manifest.version == "1.0.1"
    assert inspected.manifest.models
    assert all(model.get("device_template") for model in inspected.manifest.models)
    assert "heater.set_target" in {operation.id for operation in inspected.manifest.operations}
    assert "bambu.send_gcode" in {operation.id for operation in inspected.manifest.operations}

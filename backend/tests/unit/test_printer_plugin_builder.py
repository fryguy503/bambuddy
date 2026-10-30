"""The documented release command builds the selected source reproducibly."""

import shutil
import zipfile
from pathlib import Path

from backend.app.printers.packages import inspect_package
from scripts.build_printer_plugin import build


def test_builder_uses_release_source_and_produces_installable_identical_archives(tmp_path):
    included = Path(__file__).resolve().parents[2] / "app/printers/bambu"
    source = tmp_path / "bambu-release"
    shutil.copytree(included, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    factory = source / "plugin.py"
    factory.write_text(
        factory.read_text(encoding="utf-8").replace('version = "1.0.1"', 'version = "1.0.2"', 1), encoding="utf-8"
    )
    first, retry = tmp_path / "first.bbp", tmp_path / "retry.bbp"
    manifest = build(source, first)
    build(source, retry)
    assert manifest.version == "1.0.2"
    assert first.read_bytes() == retry.read_bytes()
    inspected = inspect_package(first.read_bytes())
    assert inspected.manifest.version == "1.0.2"
    assert inspected.manifest.models
    assert all(model.get("device_template") for model in inspected.manifest.models)
    assert "heater.set_target" in {operation.id for operation in inspected.manifest.operations}
    assert "bambu.send_gcode" in {operation.id for operation in inspected.manifest.operations}


def test_builder_produces_identical_archives_from_lf_and_crlf_source_checkouts(tmp_path):
    included = Path(__file__).resolve().parents[2] / "app/printers/bambu"
    sources = [tmp_path / "lf-source", tmp_path / "crlf-source"]
    for source, newline in zip(sources, (b"\n", b"\r\n"), strict=True):
        shutil.copytree(included, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in source.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json"}:
                canonical = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                path.write_bytes(canonical.replace(b"\n", newline))
    outputs = [tmp_path / "lf.bbp", tmp_path / "crlf.bbp"]
    manifests = [build(source, output) for source, output in zip(sources, outputs, strict=True)]
    assert manifests[0].files == manifests[1].files
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    inspect_package(outputs[0].read_bytes())
    with zipfile.ZipFile(outputs[0]) as archive:
        for name in archive.namelist():
            if name.endswith((".py", ".json")):
                assert b"\r" not in archive.read(name)
        image = "assets/printers/default.png"
        assert archive.read("payload/" + image) == (included / image).read_bytes()

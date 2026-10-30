"""Build the independent Anycubic printer package without changing application code.

From the repository root: python plugins/anycubic/build.py
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent / "payload"
sys.path.insert(0, str(ROOT))


def canonical_payload_bytes(path: Path) -> bytes:
    """Keep Python/JSON hashes identical in Windows and Linux source checkouts."""
    content = path.read_bytes()
    if path.suffix.lower() in {".py", ".json"} or path.name == "LICENSE":
        return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return content


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".anycubic-build-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build(source: Path = SOURCE, output: Path | None = None):
    from backend.app.printers.contracts import PrinterConfig
    from backend.app.printers.packages import MAX_MANIFEST_BYTES, inspect_package
    from backend.app.printers.sdk import validate_device, validate_manifest, validate_plugin_contract

    source = source.resolve(strict=True)
    if not (source / "__init__.py").is_file():
        raise ValueError("The payload must be an importable Python package")
    _atomic_write(source / "LICENSE", canonical_payload_bytes(ROOT / "LICENSE"))
    paths = sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix())
    if any(path.is_symlink() for path in paths):
        raise ValueError("Plugin payload may not contain symbolic links")
    namespace = f"bambuddy_anycubic_build_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        namespace, source / "__init__.py", submodule_search_locations=[str(source)]
    )
    if spec is None or spec.loader is None:
        raise ValueError("The payload package cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = module
    try:
        spec.loader.exec_module(module)
        plugin = importlib.import_module(f"{namespace}.plugin").AnycubicPrinterPlugin()
        validate_plugin_contract(plugin)
        if plugin.id != "anycubic" or plugin.publisher_id != "fryguy503" or plugin.api_version != 2:
            raise ValueError("Anycubic release identity or SDK version does not match this package")
        files = {
            path.relative_to(source).as_posix(): canonical_payload_bytes(path)
            for path in paths
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
            and path.name != "manifest.json"
        }
        operations, models = {}, []
        for model in plugin.models:
            descriptor = validate_device(
                plugin.describe_device(
                    PrinterConfig(
                        "192.168.50.50",
                        "BUILD-ONLY",
                        "12345678",
                        model=model.id,
                        plugin_id=plugin.id,
                        options={"tls_fingerprint": "a" * 64},
                    )
                )
            )
            for operation in descriptor.operations:
                operations.setdefault(operation.id, operation.model_dump(mode="json"))
            models.append({**model.to_dict(), "device_template": descriptor.model_dump(mode="json")})
        metadata = plugin.sdk_manifest().model_dump(mode="json")
        metadata.update(
            entrypoint="plugin:AnycubicPrinterPlugin",
            publisher_name="fryguy503",
            homepage="https://github.com/fryguy503/bambuddy/tree/codex/anycubic-printer-plugin/plugins/anycubic",
            description="Independent Anycubic signed-LAN printer provider for Bambuddy SDK v2",
            license="AGPL-3.0-only",
            dependencies={
                "aiohttp": ">=3.14.3,<4",
                "cryptography": ">=50",
                "httpx": ">=0.26,<1",
                "paho-mqtt": ">=2,<3",
            },
            models=models,
            operations=list(operations.values()),
            files={name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        )
        manifest = validate_manifest(metadata)
        encoded = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("Generated manifest exceeds the package installer limit")
        if output is None:
            output = ROOT / "packages" / "anycubic" / f"anycubic-{manifest.version}.bbp"
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".anycubic-build-", dir=output.parent)
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in [
                    ("manifest.json", encoded),
                    *[(f"payload/{name}", content) for name, content in files.items()],
                ]:
                    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, content)
            archive_bytes = Path(temporary).read_bytes()
            inspect_package(archive_bytes)
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
        _atomic_write(source.parent / "manifest.json", encoded)
        digest = hashlib.sha256(archive_bytes).hexdigest()
        _atomic_write(output.with_suffix(output.suffix + ".sha256"), f"{digest}  {output.name}\n".encode())
        return manifest
    finally:
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                sys.modules.pop(name, None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    manifest = build(arguments.source, arguments.output)
    output = arguments.output or ROOT / "packages" / "anycubic" / f"anycubic-{manifest.version}.bbp"
    print(f"Built {manifest.plugin_id} {manifest.version}: {output}")


if __name__ == "__main__":
    main()

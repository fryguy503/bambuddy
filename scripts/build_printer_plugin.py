"""Build a versioned, independently installable Bambu printer plugin archive.

Run from the repository root with the application's Python environment:
  python scripts/build_printer_plugin.py --output dist/bambu-1.0.0.bbp
Only plugin files are packaged; no application or database is replaced.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def canonical_payload_bytes(path: Path) -> bytes:
    """Package text with LF endings regardless of checkout or release host."""
    content = path.read_bytes()
    if path.suffix.lower() in {".py", ".json"}:
        return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return content


def build(source: Path, output: Path):
    from backend.app.printers.contracts import PrinterConfig
    from backend.app.printers.sdk import validate_device, validate_manifest, validate_plugin_contract

    # Build the supplied plugin source, including its own release version.
    # Relative imports resolve within that source just as they do after install.
    source = source.resolve(strict=True)
    namespace = f"bambuddy_plugin_build_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        namespace, source / "__init__.py", submodule_search_locations=[str(source)]
    )
    if spec is None or spec.loader is None:
        raise ValueError("The plugin source must be an importable Python package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = module
    spec.loader.exec_module(module)
    plugin = importlib.import_module(f"{namespace}.plugin").BambuPrinterPlugin()
    validate_plugin_contract(plugin)
    files = sorted(
        path
        for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and path.name != "manifest.json"
    )
    if any(path.is_symlink() for path in files):
        raise ValueError("Plugin payload may not contain symbolic links")
    payload = {path.relative_to(source).as_posix(): canonical_payload_bytes(path) for path in files}
    inventory = {name: hashlib.sha256(content).hexdigest() for name, content in payload.items()}
    data = plugin.sdk_manifest().model_dump(mode="json")
    operation_catalog = {}
    templates = []
    for model in plugin.models:
        descriptor = validate_device(
            plugin.describe_device(
                PrinterConfig(
                    "127.0.0.1",
                    "BUILD-ONLY",
                    "BUILD-ONLY",
                    model=model.id,
                )
            )
        )
        for operation in descriptor.operations:
            operation_catalog.setdefault(operation.id, operation.model_dump(mode="json"))
        templates.append({**model.to_dict(), "device_template": descriptor.model_dump(mode="json")})
    data.update(
        entrypoint="plugin:BambuPrinterPlugin",
        files=inventory,
        operations=list(operation_catalog.values()),
        models=templates,
    )
    manifest = validate_manifest(data)
    encoded = json.dumps(manifest.model_dump(mode="json"), separators=(",", ":"), sort_keys=True).encode() + b"\n"
    from backend.app.printers.packages import MAX_MANIFEST_BYTES

    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("Generated manifest exceeds the package installer limit")
    (source / "manifest.json").write_bytes(encoded)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:

        def write(name, content):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)

        write("manifest.json", encoded)
        for name, content in payload.items():
            write("payload/" + name, content)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "backend/app/printers/bambu")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    manifest = build(arguments.source, arguments.output)
    print(f"Built {manifest.plugin_id} {manifest.version}: {arguments.output}")


if __name__ == "__main__":
    main()

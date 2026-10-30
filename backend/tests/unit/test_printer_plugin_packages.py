"""Managed packages are inspected before execution and activate transactionally."""

import base64
import hashlib
import io
import json
import stat
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.app.printers.management import PluginManager
from backend.app.printers.packages import PluginPackageError, canonical_manifest, inspect_package
from backend.app.printers.registry import PrinterPluginRegistry


def package_bytes(
    *,
    plugin_id="example",
    version="1.0.0",
    publisher="example-org",
    api_version=1,
    marker=None,
    broken=False,
    change="",
    manifest_overrides=None,
    signing_key=None,
    key_id="vendor-key",
):
    body = (
        "from types import SimpleNamespace\n"
        "from backend.app.printers.contracts import PrinterPlugin\n"
        "from backend.app.printers.state import PrinterState\n"
        f"# {change}\n"
        f"class Example(PrinterPlugin):\n"
        f"    id = {plugin_id!r}\n"
        "    name = 'Example package'\n"
        f"    version = {version!r}\n"
        f"    api_version = {api_version!r}\n"
        "    def create_connection(self, config, **callbacks):\n"
        "        return SimpleNamespace(state=PrinterState(), callbacks=callbacks)\n"
    )
    if marker is not None:
        body += f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    if broken:
        body += "raise RuntimeError('fixture startup failure')\n"
    files = {"__init__.py": b"", "plugin.py": body.encode()}
    manifest = {
        "manifest_version": 1,
        "plugin_id": plugin_id,
        "name": "Example package",
        "version": version,
        "api_version": api_version,
        "requires_api": f">={api_version},<{api_version + 1}",
        "requires_core": ">=1.0",
        "requires_python": ">=3.11",
        "entrypoint": "plugin:Example",
        "publisher_id": publisher,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    manifest.update(manifest_overrides or {})
    signature = None
    if signing_key is not None:
        signature = {
            "algorithm": "ed25519",
            "key_id": key_id,
            "signature": base64.b64encode(signing_key.sign(canonical_manifest(manifest))).decode(),
        }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            archive.writestr("payload/" + name, data)
        if signature:
            archive.writestr("signature.json", json.dumps(signature))
    return output.getvalue()


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.printers import registry

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [])
    return PluginManager(tmp_path / "printer-plugins")


def test_staging_never_imports_code_and_restart_selects_version(managed, tmp_path):
    marker = tmp_path / "executed.txt"
    registry = PrinterPluginRegistry()
    assert registry.get("bambu").bundled
    staged = managed.stage(package_bytes(marker=marker))
    assert staged["activation"] == "staged"
    assert not staged["restart_required"] and not marker.exists()
    selected = managed.select("example", "1.0.0")
    assert selected["restart_required"] and not marker.exists()
    assert "example" not in registry._plugins  # No in-process hot replacement.
    restarted = PrinterPluginRegistry()
    assert restarted.get("example").version == "1.0.0"
    assert marker.read_text() == "executed"
    assert restarted.loaded_sources["example"]["source"] == "managed-package"


def test_identical_upload_is_idempotent_and_versions_are_immutable(managed):
    content = package_bytes()
    assert managed.stage(content) == managed.stage(content)
    with pytest.raises(PluginPackageError, match="immutable"):
        managed.stage(package_bytes(change="changed bytes"))


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/absolute.py",
        "payload/../escape.py",
        "payload/CON.py",
        "payload/a\\b.py",
        "payload/dir/file:stream",
    ],
)
def test_archive_paths_are_rejected_before_extraction(path, tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(path, "payload")
    with pytest.raises(PluginPackageError):
        inspect_package(output.getvalue())
    assert list(tmp_path.iterdir()) == []


def test_symlink_and_case_collisions_are_rejected():
    for names in (("payload/plugin.py", "payload/PLUGIN.py"), ("payload/link",)):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name in names:
                info = zipfile.ZipInfo(name)
                if name.endswith("link"):
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, b"outside")
        with pytest.raises(PluginPackageError):
            inspect_package(output.getvalue())


@pytest.mark.parametrize(
    "overrides",
    [
        {"requires_api": ">=99"},
        {"requires_core": ">=999"},
        {"requires_python": ">=99"},
        {"dependencies": {"bambuddy-nonexistent-test-dependency": ">=1"}},
        {"configuration_schema": {"$ref": "https://example.invalid/schema"}},
        {"operations": [{"id": "example.action", "label": "Invalid permission", "permission": "unregistered:scope"}]},
    ],
)
def test_incompatible_packages_never_execute(managed, tmp_path, overrides):
    marker = tmp_path / "never.txt"
    with pytest.raises(PluginPackageError, match="manifest"):
        managed.stage(package_bytes(marker=marker, manifest_overrides=overrides))
    assert not marker.exists()


def test_sdk_minor_requirement_is_checked_during_staging_before_import(managed, tmp_path, monkeypatch):
    from backend.app.printers import packages
    from backend.app.printers.sdk import SDK_API_VERSION

    supported = packages.SUPPORTED_APIS
    assert supported == ("1.0.0", SDK_API_VERSION)
    marker = tmp_path / "new-sdk-package-imported.txt"
    content = package_bytes(api_version=2, marker=marker, manifest_overrides={"requires_api": ">=2.1.0,<3.0.0"})
    monkeypatch.setattr(packages, "SUPPORTED_APIS", ("1.0.0", "2.0.0"))
    with pytest.raises(PluginPackageError, match="unsupported printer API"):
        managed.stage(content)
    assert not marker.exists()

    monkeypatch.setattr(packages, "SUPPORTED_APIS", supported)
    managed.stage(content)
    assert not marker.exists()
    # Compatible existing v2 providers do not need a package update.
    inspect_package(package_bytes(api_version=2, manifest_overrides={"requires_api": ">=2.0.0,<3.0.0"}))


@pytest.mark.parametrize("invalid_statement", ["return 1", "break", "await task()"])
def test_python_is_compiled_without_execution_during_staging(managed, tmp_path, invalid_statement):
    marker = tmp_path / "never.txt"
    with pytest.raises(PluginPackageError, match="Python source is invalid"):
        managed.stage(package_bytes(change="\n" + invalid_statement, marker=marker))
    assert not marker.exists()


def test_full_inventory_and_expected_archive_hash_are_verified():
    content = package_bytes(manifest_overrides={"files": {}})
    with pytest.raises(PluginPackageError, match="inventory"):
        inspect_package(content)
    with pytest.raises(PluginPackageError, match="SHA-256"):
        inspect_package(package_bytes(), expected_sha256="0" * 64)
    with pytest.raises(PluginPackageError, match="checksum"):
        inspect_package(package_bytes(manifest_overrides={"files": {"__init__.py": "0" * 64, "plugin.py": "0" * 64}}))


def test_bambu_update_rejects_an_unrelated_publisher(managed):
    with pytest.raises(PluginPackageError, match="publisher"):
        managed.stage(package_bytes(plugin_id="bambu", version="1.1.0", publisher="unrelated"))


def test_signed_packages_require_trusted_matching_keys_and_updates_remain_signed(managed):
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    content = package_bytes(signing_key=key)
    with pytest.raises(PluginPackageError, match="trusted"):
        managed.stage(content)
    managed.trust_key("vendor-key", "example-org", public)
    staged = managed.stage(content)
    assert staged["validation"]["signature_verified"] is True
    assert staged["validation"]["hardware_certified"] is False
    with pytest.raises(PluginPackageError, match="signature"):
        managed.stage(package_bytes(version="1.1.0"))
    managed.revoke_key("vendor-key")
    status = managed.describe(PrinterPluginRegistry())
    item = next(item for item in status["plugins"] if item["id"] == "example")
    assert item["versions"][0]["compatible"] is False
    assert item["versions"][0]["validation"]["signature_verified"] is False
    with pytest.raises(PluginPackageError, match="reassigned"):
        managed.trust_key("vendor-key", "another-publisher", public)
    with pytest.raises(PluginPackageError, match="trusted"):
        managed.select("example", "1.0.0")


def test_failed_startup_falls_back_only_to_same_plugin_known_good(managed):
    managed.stage(package_bytes())
    managed.select("example", "1.0.0")
    assert PrinterPluginRegistry().get("example").version == "1.0.0"
    managed.stage(package_bytes(version="1.1.0", broken=True))
    managed.select("example", "1.1.0", loaded_version="1.0.0")
    restarted = PrinterPluginRegistry()
    assert restarted.get("example").version == "1.0.0"
    assert "fixture startup failure" in restarted.load_errors["example"]
    status = managed.describe(restarted)["plugins"]
    item = next(item for item in status if item["id"] == "example")
    assert item["restart_required"] and item["load_error"]


def test_rollback_preserves_package_versions_and_selection(managed):
    for version in ("1.0.0", "1.1.0"):
        managed.stage(package_bytes(version=version))
        managed.select("example", version, loaded_version="1.0.0" if version == "1.1.0" else None)
        assert PrinterPluginRegistry().get("example").version == version
    result = managed.rollback("example", loaded_version="1.1.0")
    assert result["desired_version"] == "1.0.0"
    assert PrinterPluginRegistry().get("example").version == "1.0.0"
    assert len(managed.read_state()["plugins"]["example"]["versions"]) == 2


def test_disabled_and_removed_packages_never_silently_become_bambu(managed):
    from backend.app.printers.contracts import UnknownPrinterPlugin

    managed.stage(package_bytes())
    managed.select("example", "1.0.0")
    managed.set_enabled("example", False, loaded=True)
    with pytest.raises(UnknownPrinterPlugin):
        PrinterPluginRegistry().get("example")
    assert managed.remove_plugin("example")["remove_pending"]
    restarted = PrinterPluginRegistry()
    assert restarted.get("bambu").id == "bambu"
    assert "example" not in managed.read_state()["plugins"]
    assert not (managed.root / "packages" / "example").exists()


def test_payload_tampering_is_detected_at_activation_before_import(managed, tmp_path):
    marker = tmp_path / "bad.txt"
    managed.stage(package_bytes())
    payload = managed.version_path("example", "1.0.0") / "payload" / "plugin.py"
    payload.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n")
    with pytest.raises(PluginPackageError, match="integrity"):
        managed.select("example", "1.0.0")
    assert not marker.exists()


def test_archive_and_expanded_size_limits_are_enforced(monkeypatch):
    from backend.app.printers import packages

    monkeypatch.setattr(packages, "MAX_PACKAGE_BYTES", 2)
    with pytest.raises(PluginPackageError, match="upload limit"):
        inspect_package(package_bytes())
    monkeypatch.setattr(packages, "MAX_PACKAGE_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(packages, "MAX_EXPANDED_BYTES", 2)
    with pytest.raises(PluginPackageError, match="expanded"):
        inspect_package(package_bytes())


def test_selected_and_known_good_versions_cannot_be_deleted(managed):
    managed.stage(package_bytes())
    managed.select("example", "1.0.0")
    with pytest.raises(PluginPackageError, match="Selected"):
        managed.remove_version("example", "1.0.0")

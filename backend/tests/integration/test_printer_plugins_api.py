"""Installed printer adapters use the existing scoped core workflows."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.printers import api as printer_api_module, camera, files
from backend.app.printers.contracts import PrinterConfig, PrinterModel, PrinterPlugin, UnsupportedPrinterOperation
from backend.app.printers.file_types import FileListResult
from backend.app.printers.registry import PrinterPluginRegistry
from backend.app.printers.state import PrinterState
from backend.tests.integration.test_printer_scope_1727 import _admin_token, _auth, _team_member, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class TestConnection:
    __test__ = False

    def __init__(self):
        self.state = PrinterState(state="IDLE", raw_data={"device_model": "sample"})
        self.paused = False

    def connect(self, loop=None):
        self.state.connected = True

    def disconnect(self, timeout=0):
        self.state.connected = False

    def request_status_update(self):
        return True

    def check_staleness(self):
        return False

    def pause(self):
        self.paused = True
        return True


class TestPrinterPlugin(PrinterPlugin):
    """Only a test fixture: no hardware, sockets or vendor implementation."""

    __test__ = False
    id = "test_printer"
    name = "Test printer addon"
    capabilities = frozenset({"print", "pause", "files", "camera"})
    models = (PrinterModel("sample", "Sample model", capabilities=capabilities),)
    operations = ("pause_print",)

    def __init__(self):
        self.configs = []
        self.connections = []
        self.file_calls = []
        self.camera_calls = []
        self.file_configs = []
        self.camera_configs = []

    def create_connection(self, config, **callbacks):
        self.configs.append(config)
        connection = TestConnection()
        self.connections.append(connection)
        return connection

    def command_handlers(self, connection):
        return {"pause_print": connection.pause}

    def file_operation_for(self, config, operation, *args, **kwargs):
        self.file_configs.append(config)
        return super().file_operation_for(config, operation, *args, **kwargs)

    def camera_operation_for(self, config, operation, *args, **kwargs):
        self.camera_configs.append(config)
        return super().camera_operation_for(config, operation, *args, **kwargs)

    def file_operation(self, operation, *args, **kwargs):
        self.file_calls.append((operation, args, kwargs))
        if operation == "list_files_result_async":

            async def listing():
                return FileListResult(
                    files=[{"name": "plugin.gcode", "size": 12, "is_directory": False}], available=True
                )

            return listing()
        raise UnsupportedPrinterOperation(operation)

    async def camera_operation(self, operation, *args, **kwargs):
        self.camera_calls.append((operation, args, kwargs))
        if operation == "capture_camera_frame_bytes":
            return b"test-plugin-image"
        raise UnsupportedPrinterOperation(operation)


@pytest.fixture
def test_plugin(monkeypatch):
    from backend.app.printers import registry as registry_module
    from backend.app.services.printer_manager import printer_manager

    registry = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(registry_module, "registry", registry)
    registry.load()
    plugin = TestPrinterPlugin()
    registry.register(plugin)
    monkeypatch.setattr(printer_api_module, "_bindings", {})
    for name in ("_clients", "_configs", "_models", "_printer_info", "_last_trays"):
        monkeypatch.setattr(printer_manager, name, {})
    yield plugin
    for connection in plugin.connections:
        connection.disconnect()


async def test_catalog_lists_only_metadata_and_unknown_plugin_is_missing(async_client, test_plugin):
    response = await async_client.get("/api/v1/printer-plugins")
    assert response.status_code == 200
    catalog = response.json()
    assert {plugin["id"] for plugin in catalog} == {"bambu", "test_printer"}
    assert next(plugin for plugin in catalog if plugin["id"] == "bambu")["bundled"] is True
    assert next(plugin for plugin in catalog if plugin["id"] == "test_printer")["models"][0]["id"] == "sample"
    assert all("access_code" not in plugin and "plugin_options" not in plugin for plugin in catalog)
    assert (await async_client.get("/api/v1/printer-plugins/not-installed")).status_code == 404
    assert (await async_client.get("/api/v1/printer-plugins/printers/999999")).status_code == 404


async def test_unknown_plugin_creation_fails_before_any_connection_probe(async_client, test_plugin):
    with patch("backend.app.services.printer_manager.printer_manager.test_connection", new=AsyncMock()) as probe:
        response = await async_client.post(
            "/api/v1/printers/",
            json={
                "plugin_id": "not-installed",
                "name": "Unknown plugin",
                "serial_number": "case-sensitive",
                "ip_address": "192.0.2.200",
                "access_code": "do-not-probe",
            },
        )
    assert response.status_code == 422
    probe.assert_not_called()
    assert (await async_client.get("/api/v1/printers/")).json() == []


async def test_installed_plugin_creates_printer_and_dispatches_files_camera_and_commands(
    async_client, test_plugin, tmp_path, monkeypatch
):
    from backend.app.core.config import settings
    from backend.app.printers.bambu import ftp as legacy_ftp
    from backend.app.services.printer_manager import printer_manager

    created = await async_client.post(
        "/api/v1/printers/",
        json={
            "plugin_id": "test_printer",
            "name": "Plugin printer",
            "serial_number": "case-sensitive",
            "ip_address": "192.0.2.201",
            "access_code": "plugin-secret",
            "model": "sample",
            "plugin_options": {"credential": "options-secret"},
        },
    )
    assert created.status_code == 200, created.text
    printer_id = created.json()["id"]
    assert created.json()["plugin_id"] == "test_printer"
    assert created.json()["serial_number"] == "case-sensitive"
    assert len(test_plugin.connections) == 2  # Connection probe plus managed connection.
    config = test_plugin.configs[-1]
    assert config.options == {"credential": "options-secret"}
    assert "plugin-secret" not in repr(config)
    assert "options-secret" not in repr(config)

    listing = await async_client.get(f"/api/v1/printers/{printer_id}/files")
    assert listing.status_code == 200, listing.text
    assert listing.json()["files"][0]["name"] == "plugin.gcode"
    assert test_plugin.file_calls[-1][0] == "list_files_result_async"
    assert (
        await camera.capture_camera_frame_bytes(config.ip_address, config.access_code, config.model)
        == b"test-plugin-image"
    )

    connection = printer_manager.get_client(printer_id)
    assert printer_api_module.printer_api.execute(config, connection, "pause_print") is True
    assert test_plugin.connections[-1].paused
    test_plugin.connections[-1].paused = False
    paused = await async_client.post(f"/api/v1/printers/{printer_id}/print/pause")
    assert paused.status_code == 200, paused.text
    assert test_plugin.connections[-1].paused
    unsupported = await async_client.post(f"/api/v1/printers/{printer_id}/print/resume")
    assert unsupported.status_code == 501, unsupported.text
    status = await async_client.get(f"/api/v1/printers/{printer_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["connected"] is True
    assert status.json()["state"] == "IDLE"
    descriptor = await async_client.get(f"/api/v1/printer-plugins/printers/{printer_id}")
    assert descriptor.status_code == 200
    assert descriptor.json()["plugin_id"] == "test_printer"
    assert descriptor.json()["operations"] == ["pause_print"]
    assert "plugin-secret" not in descriptor.text
    assert "options-secret" not in descriptor.text

    # Shared core workflows do not require a vendor to implement its own cache.
    monkeypatch.setattr(settings, "archive_dir", tmp_path)
    source = tmp_path / "temp" / "plugin.gcode.3mf"
    source.parent.mkdir()
    source.write_bytes(b"plugin print bytes")
    files.cache_3mf_download(printer_id, "plugin.gcode.3mf", source)
    assert legacy_ftp.get_cached_3mf(printer_id, "plugin.3mf") == source
    deleted = await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=false")
    assert deleted.status_code == 200, deleted.text
    assert files.get_cached_3mf(printer_id, "plugin") is None
    assert not source.exists()


async def test_metadata_keeps_auth_permissions_printer_scope_and_secrets(async_client, printer_factory, test_plugin):
    visible = await printer_factory(
        plugin_id="test_printer",
        model="sample",
        access_code="visible-secret",
        plugin_options={"credential": "options-secret"},
    )
    hidden = await printer_factory(plugin_id="test_printer", model="sample", access_code="hidden-secret")
    admin = await _admin_token(async_client)
    reader, _ = await _team_member(async_client, admin, "plugin_reader", [visible.id])
    no_access, _ = await _user(async_client, admin, "plugin_no_read", [])

    assert (await async_client.get("/api/v1/printer-plugins")).status_code == 401
    denied = await async_client.get("/api/v1/printer-plugins", headers=_auth(no_access))
    assert denied.status_code == 403
    allowed = await async_client.get(f"/api/v1/printer-plugins/printers/{visible.id}", headers=_auth(reader))
    assert allowed.status_code == 200
    assert allowed.json()["plugin_id"] == "test_printer"
    assert "visible-secret" not in allowed.text and "options-secret" not in allowed.text
    assert (
        await async_client.get(f"/api/v1/printer-plugins/printers/{hidden.id}", headers=_auth(reader))
    ).status_code == 404

    printer = await async_client.get(f"/api/v1/printers/{visible.id}", headers=_auth(reader))
    assert printer.status_code == 200
    assert "access_code" not in printer.json()
    assert "plugin_options" not in printer.json()
    assert "options-secret" not in printer.text


async def test_core_retry_does_not_require_plugin_specific_transport_cooldown(test_plugin):
    config = PrinterConfig("192.0.2.205", "case-sensitive", "secret", "sample", "test_printer", 205)
    printer_api_module.bind_printer(config)
    attempts = []

    async def operation():
        attempts.append(1)
        return len(attempts) == 2

    assert await files.with_ftp_retry(operation, max_retries=1, retry_delay=0, cooloff_ip=config.ip_address) is True
    assert len(attempts) == 2


async def test_offline_printer_file_and_camera_hooks_receive_persisted_options(
    async_client, printer_factory, db_session, test_plugin
):
    from backend.app.services.printer_manager import init_printer_connections, printer_manager

    printer = await printer_factory(
        plugin_id="test_printer",
        model="sample",
        is_active=False,
        plugin_options={"file_token": "offline-file-secret", "camera_token": "offline-camera-secret"},
    )
    await init_printer_connections(db_session)
    assert not test_plugin.connections
    assert printer_manager.get_client(printer.id) is None

    listing = await async_client.get(f"/api/v1/printers/{printer.id}/files")
    assert listing.status_code == 200, listing.text
    assert test_plugin.file_configs[-1].printer_id == printer.id
    assert test_plugin.file_configs[-1].options["file_token"] == "offline-file-secret"
    assert (
        await camera.capture_camera_frame_bytes(printer.ip_address, printer.access_code, printer.model)
        == b"test-plugin-image"
    )
    assert test_plugin.camera_configs[-1].options["camera_token"] == "offline-camera-secret"

    # Direct addon API callers receive the same config without a live connection.
    direct = await printer_api_module.printer_api.files(
        printer, "list_files_result_async", printer.ip_address, printer.access_code, "/"
    )
    assert direct.available
    await printer_api_module.printer_api.camera(
        printer, "capture_camera_frame_bytes", printer.ip_address, printer.access_code, printer.model
    )
    assert test_plugin.file_configs[-1].options == printer.plugin_options
    assert test_plugin.camera_configs[-1].options == printer.plugin_options
    assert "offline-file-secret" not in listing.text
    assert "offline-camera-secret" not in listing.text

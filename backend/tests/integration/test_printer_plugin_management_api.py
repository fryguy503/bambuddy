"""Only administrators can stage executable packages or change plugin selection."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from backend.app.printers.management import PluginManager
from backend.app.printers.registry import PrinterPluginRegistry
from backend.tests.integration.test_printer_scope_1727 import _admin_token, _auth, _group, _user
from backend.tests.unit.test_printer_plugin_packages import package_bytes

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def management_api(tmp_path, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.printers import registry

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(registry, "registry", PrinterPluginRegistry())
    return PluginManager(tmp_path / "printer-plugins")


async def test_stage_select_and_status_are_truthful_about_restart(async_client, management_api, tmp_path):
    marker = tmp_path / "executed.txt"
    response = await async_client.post(
        "/api/v1/printer-plugins/packages",
        files={"file": ("example.bbp", package_bytes(marker=marker), "application/zip")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["activation"] == "staged"
    assert response.json()["validation"]["hardware_certified"] is False
    assert not marker.exists()
    staged = (await async_client.get("/api/v1/printer-plugins/management")).json()
    item = next(item for item in staged["plugins"] if item["id"] == "example")
    assert item["loaded_version"] is None and item["desired_version"] is None
    assert not item["restart_required"]
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] and not marker.exists()
    status = (await async_client.get("/api/v1/printer-plugins/management")).json()
    item = next(item for item in status["plugins"] if item["id"] == "example")
    assert item["loaded_version"] is None and item["desired_version"] == "1.0.0"
    assert item["restart_required"]


async def test_management_requires_actual_admin_not_only_settings_permission(async_client, management_api):
    admin = await _admin_token(async_client)
    group = await _group(async_client, admin, "plugin_operators", permissions=["settings:update", "printers:read"])
    operator, _ = await _user(async_client, admin, "plugin_operator", [group])
    assert (await async_client.get("/api/v1/printer-plugins/management")).status_code == 401
    assert (await async_client.get("/api/v1/printer-plugins/management", headers=_auth(operator))).status_code == 403
    denied = await async_client.post(
        "/api/v1/printer-plugins/packages",
        headers=_auth(operator),
        files={"file": ("example.bbp", package_bytes(), "application/zip")},
    )
    assert denied.status_code == 403
    assert not management_api.state_path.exists()
    assert (await async_client.get("/api/v1/printer-plugins/management", headers=_auth(admin))).status_code == 200
    # Regular metadata remains readable without exposing management details.
    assert (await async_client.get("/api/v1/printer-plugins", headers=_auth(operator))).status_code == 200


async def test_invalid_upload_is_rejected_before_import(async_client, management_api, tmp_path):
    marker = tmp_path / "never.txt"
    response = await async_client.post(
        "/api/v1/printer-plugins/packages",
        files={
            "file": (
                "example.bbp",
                package_bytes(marker=marker, manifest_overrides={"requires_core": ">=999"}),
                "application/zip",
            )
        },
    )
    assert response.status_code == 409, response.text
    assert not marker.exists()
    assert not management_api.state_path.exists()


async def test_active_print_prevents_selection_and_configured_printers_prevent_disable(
    async_client,
    management_api,
    printer_factory,
):
    from backend.app.services.printer_manager import printer_manager

    management_api.stage(package_bytes())
    printer = await printer_factory(plugin_id="example", name="Managed printer")
    state = SimpleNamespace(state="RUNNING", connected=True)
    with patch.object(printer_manager, "get_status", return_value=state):
        response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 409
    assert management_api.read_state()["plugins"]["example"]["desired_version"] is None
    disabled = await async_client.post("/api/v1/printer-plugins/example/disable")
    assert disabled.status_code == 409
    removed = await async_client.delete("/api/v1/printer-plugins/example")
    assert removed.status_code == 409
    assert printer.id > 0


async def test_inflight_upload_prevents_selection(async_client, management_api, printer_factory):
    from backend.app.services.print_scheduler import scheduler as print_scheduler

    management_api.stage(package_bytes())
    printer = await printer_factory(plugin_id="example", name="Uploading printer")
    task = SimpleNamespace(done=lambda: False)
    with patch.object(print_scheduler, "_inflight", {123: (task, printer.id)}):
        response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 409
    assert "upload" in response.json()["detail"]


async def test_running_sdk_operation_blocks_upgrade(async_client, management_api, printer_factory, db_session):
    from backend.app.models.printer_operation import PrinterOperation

    management_api.stage(package_bytes())
    printer = await printer_factory(plugin_id="example", name="Calibrating printer")
    db_session.add(
        PrinterOperation(
            id=str(uuid4()),
            printer_id=printer.id,
            operation_id="example.calibrate",
            idempotency_key="calibration",
            request_digest="0" * 64,
            request_data={},
            operation_schema={},
            receipt={"status": "running"},
            provider_task_id="calibration-1",
            process_id=str(uuid4()),
        )
    )
    await db_session.commit()
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 409, response.text
    assert "SDK operation" in response.json()["detail"]


async def test_unselected_version_can_be_removed_but_bundle_cannot(async_client, management_api):
    management_api.stage(package_bytes())
    response = await async_client.delete("/api/v1/printer-plugins/example/versions/1.0.0")
    assert response.status_code == 200 and response.json()["removed"]
    response = await async_client.delete("/api/v1/printer-plugins/bambu")
    assert response.status_code == 409


async def test_upload_limit_applies_before_multipart_spooling(async_client, management_api, monkeypatch):
    from backend.app.api.routes import printer_plugins

    monkeypatch.setattr(printer_plugins, "MAX_PACKAGE_BYTES", 4)
    response = await async_client.post(
        "/api/v1/printer-plugins/packages",
        content=b"x",
        headers={"Content-Length": str(2 * 1024 * 1024)},
    )
    assert response.status_code == 413
    assert not management_api.state_path.exists()


async def test_admin_api_key_cannot_install_executable_plugins(async_client, management_api):
    admin = await _admin_token(async_client)
    created = await async_client.post("/api/v1/api-keys/", headers=_auth(admin), json={"name": "admin-owned-key"})
    assert created.status_code == 200
    key_headers = {"X-API-Key": created.json()["key"]}
    response = await async_client.post(
        "/api/v1/printer-plugins/packages",
        headers=key_headers,
        files={"file": ("example.bbp", package_bytes(), "application/zip")},
    )
    assert response.status_code == 403
    assert not management_api.state_path.exists()


async def test_candidate_schema_checks_saved_settings_without_execution_or_data_changes(
    async_client,
    management_api,
    printer_factory,
    db_session,
    tmp_path,
):
    marker = tmp_path / "never.txt"
    printer = await printer_factory(plugin_id="example", plugin_options={"token": 789123})
    management_api.stage(
        package_bytes(
            marker=marker,
            manifest_overrides={
                "configuration_schema": {
                    "type": "object",
                    "properties": {"token": {"type": "string"}},
                    "required": ["token"],
                    "additionalProperties": False,
                }
            },
        )
    )
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 409
    assert "incompatible" in response.json()["detail"]
    assert "789123" not in response.text
    assert not marker.exists()
    assert management_api.read_state()["plugins"]["example"]["desired_version"] is None
    await db_session.refresh(printer)
    assert printer.plugin_options == {"token": 789123}


async def test_chunked_upload_limit_cannot_be_bypassed(async_client, management_api, monkeypatch):
    from backend.app.api.routes import printer_plugins

    monkeypatch.setattr(printer_plugins, "MAX_PACKAGE_BYTES", 4)
    payload = (
        b'--limit\r\nContent-Disposition: form-data; name="file"; filename="test.bbp"\r\n'
        b"Content-Type: application/zip\r\n\r\n" + b"x" * (1024 * 1024 + 100) + b"\r\n--limit--\r\n"
    )

    async def chunks():
        for offset in range(0, len(payload), 65536):
            yield payload[offset : offset + 65536]

    response = await async_client.post(
        "/api/v1/printer-plugins/packages",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=limit"},
    )
    assert response.status_code == 413, response.text
    assert not management_api.state_path.exists()

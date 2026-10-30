"""Only administrators can stage executable packages or change plugin selection."""

import hashlib
import io
import json
import warnings
import zipfile
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
    from backend.app.printers import lifecycle, registry, rolling

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(registry, "registry", PrinterPluginRegistry())
    monkeypatch.setattr(lifecycle, "lifecycle", lifecycle.PluginLifecycle())
    monkeypatch.setattr(rolling, "rollouts", rolling.RolloutCoordinator())
    return PluginManager(tmp_path / "printer-plugins")


def _operation_record(printer_id, status="running"):
    from backend.app.models.printer_operation import PrinterOperation

    return PrinterOperation(
        id=str(uuid4()),
        printer_id=printer_id,
        operation_id="example.calibrate",
        idempotency_key="calibration",
        request_digest="0" * 64,
        request_data={},
        operation_schema={},
        receipt={"status": status},
        provider_task_id="calibration-1",
        process_id=str(uuid4()),
    )


async def test_openapi_includes_selected_printer_extensions_without_duplicate_live_routes(async_client, management_api):
    from fastapi.openapi.utils import get_openapi

    from backend.app.main import app

    routes = tuple(app.routes)
    with warnings.catch_warnings(record=True) as core_warnings:
        warnings.simplefilter("always")
        get_openapi(title=app.title, version=app.version, routes=app.routes)
    with warnings.catch_warnings(record=True) as documented_warnings:
        warnings.simplefilter("always")
        response = await async_client.get("/openapi.json")
    assert response.status_code == 200, response.text
    paths = response.json()["paths"]
    assert "/api/v1/printers/{printer_id}/temperature/nozzle" in paths
    assert "/api/v1/cloud/status" in paths
    assert "/api/v1/printer-plugins/{plugin_id}/activate" in paths
    assert tuple(app.routes) == routes
    # Existing core endpoints can expose more than one HTTP method under one
    # operation ID. Adding plugin docs must not introduce further duplicates.
    original_duplicates = {str(item.message) for item in core_warnings if "Duplicate Operation ID" in str(item.message)}
    assert {
        str(item.message) for item in documented_warnings if "Duplicate Operation ID" in str(item.message)
    } <= original_duplicates


async def test_staging_never_imports_and_activation_loads_without_restart(async_client, management_api, tmp_path):
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
    assert response.json()["restart_required"] is False and marker.exists()
    status = (await async_client.get("/api/v1/printer-plugins/management")).json()
    item = next(item for item in status["plugins"] if item["id"] == "example")
    assert item["loaded_version"] == "1.0.0" and item["desired_version"] == "1.0.0"
    assert item["restart_required"] is False
    assert item["rollout"]["status"] == "current"
    assert item["running_versions"][0]["version"] == "1.0.0"


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


async def test_active_print_allows_refresh_but_configured_printers_prevent_disable(
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
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False
    assert management_api.read_state()["plugins"]["example"]["desired_version"] == "1.0.0"
    disabled = await async_client.post("/api/v1/printer-plugins/example/disable")
    assert disabled.status_code == 409
    removed = await async_client.delete("/api/v1/printer-plugins/example")
    assert removed.status_code == 409
    assert printer.id > 0


async def test_inflight_upload_does_not_prevent_monitor_refresh(async_client, management_api, printer_factory):
    from backend.app.services.print_scheduler import scheduler as print_scheduler

    management_api.stage(package_bytes())
    printer = await printer_factory(plugin_id="example", name="Uploading printer")
    task = SimpleNamespace(done=lambda: False)
    with patch.object(print_scheduler, "_inflight", {123: (task, printer.id)}):
        response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False
    assert not task.done()


@pytest.mark.parametrize("active_status", ["printing", "uploading", "starting", "paused"])
async def test_refresh_preserves_active_and_unrelated_queue_jobs(
    async_client, management_api, printer_factory, db_session, active_status
):
    from backend.app.models.print_queue import PrintQueueItem

    management_api.stage(package_bytes())
    management_api.stage(package_bytes(version="1.1.0"))
    management_api.select("example", "1.0.0")  # Load a real provider before testing an upgrade.
    affected = await printer_factory(plugin_id="example", name="Affected printer")
    unrelated = await printer_factory(plugin_id="bambu", name="Unrelated printer")
    unrelated_job = PrintQueueItem(printer_id=unrelated.id, status=active_status)
    cancelled_job = PrintQueueItem(printer_id=affected.id, status="cancelled")
    db_session.add_all([unrelated_job, cancelled_job])
    await db_session.commit()

    # Neither active nor historical jobs are deleted or rewritten by selection.
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 200, response.text

    active_job = PrintQueueItem(printer_id=affected.id, status=active_status)
    db_session.add(active_job)
    await db_session.commit()
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.1.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False
    assert management_api.read_state()["plugins"]["example"]["desired_version"] == "1.1.0"
    await db_session.refresh(active_job)
    await db_session.refresh(unrelated_job)
    await db_session.refresh(cancelled_job)
    assert active_job.status == active_status  # Inspection never cancels a job.
    assert unrelated_job.status == active_status and cancelled_job.status == "cancelled"


async def test_running_sdk_operation_does_not_block_monitor_refresh(
    async_client, management_api, printer_factory, db_session
):
    management_api.stage(package_bytes())
    management_api.select("example", "1.0.0")
    printer = await printer_factory(plugin_id="example", name="Calibrating printer")
    db_session.add(_operation_record(printer.id))
    await db_session.commit()
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False


@pytest.mark.parametrize("operation_status", ["accepted", "running"])
async def test_first_activation_restores_missing_provider_without_cancelling_durable_jobs(
    async_client, management_api, printer_factory, db_session, tmp_path, operation_status
):
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.printers.registry import registry
    from backend.app.services.printer_manager import printer_manager

    marker = tmp_path / "recovery-import.txt"
    management_api.stage(package_bytes(marker=marker))
    printer = await printer_factory(plugin_id="example", name="Migrated printer")
    queue_job = PrintQueueItem(printer_id=printer.id, status="printing")
    operation = _operation_record(printer.id, operation_status)
    db_session.add_all([queue_job, operation])
    await db_session.commit()

    with (
        patch.object(printer_manager, "get_status", return_value=None),
        patch.object(printer_manager, "get_client", return_value=None),
    ):
        response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False
    assert management_api.read_state()["plugins"]["example"]["desired_version"] == "1.0.0"
    assert "example" in registry._plugins
    assert marker.exists()  # Explicit activation imports trusted code; it does not dispatch a job.
    await db_session.refresh(queue_job)
    await db_session.refresh(operation)
    assert queue_job.status == "printing"
    assert operation.receipt["status"] == operation_status


@pytest.mark.parametrize("blocker", ["queue", "operation"])
async def test_refresh_retains_existing_provider_pin_with_active_durable_work(
    async_client, management_api, printer_factory, db_session, blocker
):
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.printers.registry import registry
    from backend.app.services.printer_manager import printer_manager

    management_api.stage(package_bytes())
    management_api.stage(package_bytes(version="1.1.0"))
    management_api.select("example", "1.0.0")
    old_plugin = registry.get("example")
    printer = await printer_factory(plugin_id="example", name="Existing connection")
    record = (
        PrintQueueItem(printer_id=printer.id, status="printing")
        if blocker == "queue"
        else _operation_record(printer.id)
    )
    db_session.add(record)
    await db_session.commit()
    with (
        patch.object(printer_manager, "get_status", return_value=None),
        patch.object(printer_manager, "get_client", return_value=SimpleNamespace(_plugin=old_plugin)),
    ):
        response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.1.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False
    assert registry.get("example").version == "1.1.0"
    assert registry.provider_for(printer) is old_plugin
    assert management_api.get_printer_pins()[str(printer.id)]["version"] == "1.0.0"
    await db_session.refresh(record)
    assert (record.status if blocker == "queue" else record.receipt["status"]) in {"printing", "running"}


async def test_rollback_refreshes_with_active_queue_jobs(async_client, management_api, printer_factory, db_session):
    from backend.app.models.print_queue import PrintQueueItem

    management_api.stage(package_bytes())
    printer = await printer_factory(plugin_id="example", name="Unloaded provider")
    db_session.add(PrintQueueItem(printer_id=printer.id, status="printing"))
    await db_session.commit()
    response = await async_client.post("/api/v1/printer-plugins/example/rollback", json={"version": "1.0.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False
    assert management_api.read_state()["plugins"]["example"]["desired_version"] == "1.0.0"


async def test_live_dispatch_does_not_block_provider_activation(async_client, management_api, printer_factory):
    from backend.app.services.print_scheduler import scheduler as print_scheduler

    management_api.stage(package_bytes())
    await printer_factory(plugin_id="example", name="Dispatching printer")
    with patch.object(print_scheduler, "_printer_in_dispatch_hold", return_value=True):
        response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 200, response.text
    assert response.json()["restart_required"] is False
    assert management_api.read_state()["plugins"]["example"]["desired_version"] == "1.0.0"


async def test_bad_candidate_import_preserves_running_default_and_selection(async_client, management_api):
    from backend.app.printers.registry import registry

    management_api.stage(package_bytes())
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 200
    previous = registry.get("example")
    management_api.stage(package_bytes(version="2.0.0", broken=True))
    checkpoint = management_api.selection_checkpoint("example")
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "2.0.0"})
    assert response.status_code == 409, response.text
    assert registry.get("example") is previous
    assert management_api.selection_checkpoint("example") == checkpoint


async def test_failed_first_service_start_does_not_publish_or_select_candidate(async_client, management_api):
    from backend.app.printers.registry import registry

    with zipfile.ZipFile(io.BytesIO(package_bytes())) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files["payload/plugin.py"] += (
        b"\nasync def fail_start(self, host):\n    raise RuntimeError('PRIVATE-SERVICE-DETAIL')\n"
        b"Example.start_host_services = fail_start\n"
    )
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["plugin.py"] = hashlib.sha256(files["payload/plugin.py"]).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    management_api.stage(output.getvalue())
    checkpoint = management_api.selection_checkpoint("example")
    response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": "1.0.0"})
    assert response.status_code == 409, response.text
    assert "PRIVATE-SERVICE-DETAIL" not in response.text
    assert "example" not in registry._plugins
    assert management_api.selection_checkpoint("example") == checkpoint


async def test_pending_receipt_retains_origin_beyond_multiple_default_updates(
    async_client, management_api, printer_factory, db_session, monkeypatch
):
    from backend.app.printers.contracts import PrinterConfig
    from backend.app.printers.management import printer_pin_identity
    from backend.app.printers.registry import registry

    for version in ("1.0.0", "2.0.0", "3.0.0"):
        management_api.stage(package_bytes(version=version))
    management_api.select("example", "1.0.0")
    registry.load()
    printer = await printer_factory(plugin_id="example", name="Printing while monitoring refreshes")
    operation = _operation_record(printer.id)
    db_session.add(operation)
    await db_session.commit()
    for version in ("2.0.0", "3.0.0"):
        response = await async_client.post("/api/v1/printer-plugins/example/activate", json={"version": version})
        assert response.status_code == 200, response.text
    await db_session.refresh(operation)
    binding = operation.operation_schema["extensions"]["bambuddy:provider"]
    assert binding == {
        "plugin_id": "example",
        "version": "1.0.0",
        "identity": printer_pin_identity(PrinterConfig.from_printer(printer)),
    }
    assert operation.receipt["status"] == "running"
    assert management_api.read_state()["plugins"]["example"]["previous_version"] == "2.0.0"
    assert "1.0.0" in registry.protected_runtime_versions("example")
    response = await async_client.delete("/api/v1/printer-plugins/example/versions/1.0.0")
    assert response.status_code == 409, response.text
    # A process restart restores the task's old origin independently of the
    # current default and its most recent rollback version.
    from backend.app.core.database import async_session
    from backend.app.printers import registry as registry_module
    from backend.app.printers.lifecycle import PluginLifecycle

    restarted = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", restarted)
    lifecycle = PluginLifecycle()
    lifecycle._host = SimpleNamespace(async_session=async_session)
    await lifecycle._restore_pending_operations()
    assert restarted.get("example").version == "3.0.0"
    assert "1.0.0" in restarted.protected_runtime_versions("example")


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

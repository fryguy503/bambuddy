"""Queue plans survive persistence and dispatch through a six-tool USB provider."""

from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.printers import camera, files
from backend.app.printers.contracts import PrinterConfig, PrinterPluginError
from backend.app.printers.job_dispatch import queue_job_plan, start_queue_job
from backend.app.printers.sdk import DeviceDescriptor, JobArtifact, JobRequest, MaterialBinding, ResourceDescriptor
from backend.tests.integration.test_printer_devices_api import create_sdk_printer, sdk_plugin as sdk_plugin
from backend.tests.integration.test_printer_scope_1727 import _admin_token, _auth, _group, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def queue_plugin(sdk_plugin, monkeypatch):
    original = sdk_plugin.describe_device

    def descriptor(config):
        device = original(config)
        device.resources.append(
            ResourceDescriptor(
                id="source:violet",
                kind="material_source",
                label="Violet material feed",
                relations={"tools": ["tool:violet"]},
            )
        )
        device.resource("tool:violet").relations["material_sources"] = ["source:violet"]
        return DeviceDescriptor.model_validate(device.model_dump())

    async def file_operation(config, operation, *args, **kwargs):
        assert operation == "upload_file_async"
        sdk_plugin.uploads.append((config, Path(args[2]).read_bytes(), args[3]))
        return True

    async def camera_operation(config, operation, *args, **kwargs):
        assert operation == "capture_camera_frame_bytes"
        sdk_plugin.captured_options = dict(config.options)
        return b"usb-camera-frame"

    sdk_plugin.uploads = []
    monkeypatch.setattr(sdk_plugin, "describe_device", descriptor)
    monkeypatch.setattr(sdk_plugin, "file_operation_for", file_operation)
    monkeypatch.setattr(sdk_plugin, "camera_operation_for", camera_operation)
    return sdk_plugin


async def test_six_tool_source_binding_survives_queue_crud_upload_and_dispatch(
    async_client, queue_plugin, archive_factory, db_session, tmp_path, monkeypatch
):
    from backend.app.core.config import settings
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    content = b"; SDK fixture, no physical printer\nG28\n"
    artifact = tmp_path / "six-tools.gcode"
    artifact.write_bytes(content)
    printer_id = await create_sdk_printer(async_client)
    archive = await archive_factory(printer_id, filename="six-tools.gcode", file_path="six-tools.gcode", with_run=False)
    plan = JobRequest(
        job_id="queue-fixture",
        artifact=JobArtifact(uri="host://archive/source", format="gcode"),
        selected_tool_ids=["tool:violet"],
        bindings=[MaterialBinding(input_id="material-a", tool_id="tool:violet", source_id="source:violet")],
        options={"quality": "fine"},
    )
    created = await async_client.post(
        "/api/v1/queue/",
        json={
            "printer_id": printer_id,
            "archive_id": archive.id,
            "manual_start": True,
            "tool_ids": ["tool:violet"],
            "material_bindings": {"material-a": "source:violet"},
            "job_options": {"quality": "fine"},
            "sdk_job": plan.model_dump(mode="json"),
        },
    )
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]
    read = await async_client.get(f"/api/v1/queue/{item_id}")
    assert read.status_code == 200, read.text
    assert read.json()["tool_ids"] == ["tool:violet"]
    assert read.json()["material_bindings"] == {"material-a": "source:violet"}
    assert read.json()["sdk_job"]["bindings"][0]["source_id"] == "source:violet"
    plan.options["quality"] = "draft"
    updated = await async_client.patch(
        f"/api/v1/queue/{item_id}", json={"job_options": {"quality": "draft"}, "sdk_job": plan.model_dump(mode="json")}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["sdk_job"]["options"]["quality"] == "draft"
    item = await db_session.scalar(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    printer = await db_session.scalar(select(Printer).where(Printer.id == printer_id))
    assert item.sdk_job["selected_tool_ids"] == ["tool:violet"]
    assert await files.upload_file_async("", "", artifact, "prepared.gcode", printer_id=printer_id)
    assert queue_plugin.uploads[-1][1:] == (content, "prepared.gcode")
    assert queue_plugin.uploads[-1][0].options["token"] == "CONFIG-SECRET"
    assert await start_queue_job(item, printer, printer_manager.get_client(printer_id), "prepared.gcode")
    request = queue_plugin.calls[-1]
    assert request.operation_id == "job.start"
    assert request.parameters["artifact"]["uri"] == "prepared.gcode"
    assert request.parameters["bindings"][0]["source_id"] == "source:violet"
    assert request.parameters["selected_tool_ids"] == ["tool:violet"]
    assert request.parameters["options"] == {"quality": "draft"}
    assert queue_plugin.connections[-1].state.job.active_tool_ids == ["tool:violet"]
    assert await camera.capture_camera_frame_bytes("", "", "simulator", printer_id=printer_id) == b"usb-camera-frame"
    assert queue_plugin.captured_options["token"] == "CONFIG-SECRET"
    with pytest.raises(PrinterPluginError):
        await files.upload_file_async("", "", artifact, "unbound.gcode")
    with pytest.raises(PrinterPluginError):
        await camera.capture_camera_frame_bytes("", "", "simulator")


async def test_queue_plan_requires_explicit_tools_and_unambiguous_resource_paths(async_client, queue_plugin):
    from types import SimpleNamespace

    from backend.app.printers.sdk import SDKValidationError
    from backend.app.services.printer_manager import printer_manager

    printer_id = await create_sdk_printer(async_client)
    config = printer_manager._configs[printer_id]
    empty = SimpleNamespace(id=11, sdk_job=None, tool_ids=None, material_bindings=None, job_options=None, plate_id=None)
    with pytest.raises(SDKValidationError):
        queue_job_plan(empty, config, "prepared.gcode")
    item = SimpleNamespace(
        id=12,
        sdk_job=None,
        tool_ids=["tool:violet"],
        material_bindings={"material-a": "source:violet"},
        job_options={"quality": "fine"},
        plate_id=None,
    )
    plan = queue_job_plan(item, config, "prepared.gcode")
    assert plan.bindings[0].tool_id == "tool:violet"
    item.material_bindings = {"material-a": "source:missing"}
    calls = len(queue_plugin.calls)
    with pytest.raises(ValueError):
        queue_job_plan(item, config, "prepared.gcode")
    assert len(queue_plugin.calls) == calls


async def test_queue_scope_and_job_admission_remain_host_authorized(
    async_client, queue_plugin, printer_factory, archive_factory
):
    printer_id = await create_sdk_printer(async_client)
    hidden = await printer_factory(plugin_id=queue_plugin.id, model="simulator", plugin_options={"token": "hidden"})
    own_archive = await archive_factory(printer_id, with_run=False)
    hidden_archive = await archive_factory(hidden.id, with_run=False)
    admin = await _admin_token(async_client)
    group = await _group(
        async_client,
        admin,
        "sdkqueue-operator",
        permissions=[
            "printers:read",
            "printers:control",
            "queue:create",
            "queue:read_all",
            "archives:read_all",
            "archives:reprint_all",
        ],
        printer_ids=[printer_id],
    )
    operator, _ = await _user(async_client, admin, "sdkqueueoperator", [group])
    denied = await async_client.post(
        "/api/v1/queue/",
        headers=_auth(operator),
        json={"printer_id": hidden.id, "archive_id": hidden_archive.id, "tool_ids": ["tool:violet"]},
    )
    assert denied.status_code == 404
    allowed = await async_client.post(
        "/api/v1/queue/",
        headers=_auth(operator),
        json={
            "printer_id": printer_id,
            "archive_id": own_archive.id,
            "manual_start": True,
            "tool_ids": ["tool:violet"],
            "material_bindings": {"material-a": "source:violet"},
        },
    )
    assert allowed.status_code == 200, allowed.text
    assert not queue_plugin.calls


async def test_invalid_resource_plans_are_rejected_before_queue_or_batch_mutation(
    async_client, queue_plugin, archive_factory, db_session
):
    from sqlalchemy import func

    from backend.app.models.print_batch import PrintBatch
    from backend.app.models.print_queue import PrintQueueItem

    printer_id = await create_sdk_printer(async_client)
    archive = await archive_factory(printer_id, with_run=False)
    before_items = await db_session.scalar(select(func.count()).select_from(PrintQueueItem))
    before_batches = await db_session.scalar(select(func.count()).select_from(PrintBatch))
    payload = {"printer_id": printer_id, "archive_id": archive.id, "quantity": 2, "manual_start": True}
    for invalid in (
        {"tool_ids": ["tool:missing"]},
        {"tool_ids": ["tool:0"], "material_bindings": {"a": "source:violet"}},
        {"sdk_job": {"job_id": "missing-artifact"}},
    ):
        response = await async_client.post("/api/v1/queue/", json={**payload, **invalid})
        assert response.status_code == 400, response.text
    assert await db_session.scalar(select(func.count()).select_from(PrintQueueItem)) == before_items
    assert await db_session.scalar(select(func.count()).select_from(PrintBatch)) == before_batches
    assert not queue_plugin.calls

    valid = await async_client.post(
        "/api/v1/queue/",
        json={"printer_id": printer_id, "archive_id": archive.id, "manual_start": True, "tool_ids": ["tool:violet"]},
    )
    assert valid.status_code == 200, valid.text
    item_id = valid.json()["id"]
    rejected = await async_client.patch(f"/api/v1/queue/{item_id}", json={"tool_ids": ["tool:missing"]})
    assert rejected.status_code == 400, rejected.text
    unchanged = await async_client.get(f"/api/v1/queue/{item_id}")
    assert unchanged.json()["tool_ids"] == ["tool:violet"]
    unassigned = await async_client.patch(
        f"/api/v1/queue/{item_id}", json={"printer_id": None, "tool_ids": ["tool:violet"]}
    )
    assert unassigned.status_code == 400, unassigned.text

"""Native uploads stay upload-only and retain their provider through refresh."""

import asyncio
from types import MethodType
from uuid import uuid4

import pytest

from backend.app.api.routes import printer_uploads
from backend.app.core.config import settings
from backend.app.printers.registry import RuntimeProvider, current_registry
from backend.app.services.printer_file_uploads import PrinterFileUploads
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_printer_devices_api import (
    SixToolPlugin,
    create_sdk_printer,
    sdk_plugin as sdk_plugin,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def uploads(sdk_plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "plugins")
    monkeypatch.setattr(printer_manager, "_retained_clients", {})
    monkeypatch.setattr(printer_manager, "_connection_tokens", {})
    monkeypatch.setattr(sdk_plugin, "model_capabilities", lambda _model: frozenset({"print", "file_upload"}))
    service = PrinterFileUploads()
    monkeypatch.setattr(printer_uploads, "printer_file_uploads", service)
    yield service
    await service.shutdown()


@pytest.mark.parametrize("phase", ["printing", "paused"])
async def test_upload_during_print_and_refresh_uses_original_provider_once(
    async_client, sdk_plugin, uploads, db_session, phase
):
    from backend.app.models.printer import Printer

    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def upload(self, config, operation, *args, **kwargs):
        assert operation == "upload_file_async"
        assert current_registry().provider_for(config) is self
        path = args[2]
        calls.append((self, path, path.read_bytes(), args[3]))
        kwargs["progress_callback"](path.stat().st_size, path.stat().st_size)
        entered.set()
        await release.wait()
        assert current_registry().provider_for(config) is self
        return True

    sdk_plugin.file_operation_for = MethodType(upload, sdk_plugin)
    printer_id = await create_sdk_printer(async_client)
    original = printer_manager.get_client(printer_id)
    original._connection.state.status = phase
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": uuid4().hex},
        files={"file": ("part.gcode", b"G1 X10\n")},
    )
    assert response.status_code == 202, response.text
    await entered.wait()
    job_id = response.json()["id"]
    assert uploads.get(printer_id, job_id).phase == "verifying"

    registry = current_registry()
    updated = SixToolPlugin()
    updated.version = "1.1.0"
    registry.activate(
        RuntimeProvider(updated, updated.version, {"version": updated.version, "source": "managed-package"})
    )
    printer = await db_session.get(Printer, printer_id)
    prepared = printer_manager.prepare_connection(printer, updated)
    printer_manager.publish_connection(prepared, retain_old=True)
    assert printer_manager.get_client(printer_id)._plugin is updated
    assert sdk_plugin.version in registry.protected_runtime_versions(sdk_plugin.id)
    release.set()
    await uploads.get(printer_id, job_id).task
    result = await async_client.get(f"/api/v1/printers/{printer_id}/files/uploads/{job_id}")
    assert result.json()["phase"] == "completed"
    assert len(calls) == 1 and calls[0][0] is sdk_plugin
    assert calls[0][2:] == (b"G1 X10\n", "part.gcode")
    assert not calls[0][1].exists()
    assert sdk_plugin.calls == [] and updated.calls == []


async def test_upload_cancel_releases_provider_and_removes_staging_file(async_client, sdk_plugin, uploads):
    entered = asyncio.Event()
    staged = []

    async def upload(self, config, operation, *args, **kwargs):
        staged.append(args[2])
        entered.set()
        await asyncio.Event().wait()

    sdk_plugin.file_operation_for = MethodType(upload, sdk_plugin)
    printer_id = await create_sdk_printer(async_client)
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": uuid4().hex},
        files={"file": ("part.gcode", b"G1 X10\n")},
    )
    await entered.wait()
    job_id = response.json()["id"]
    hidden = await async_client.get(f"/api/v1/printers/{printer_id + 1}/files/uploads/{job_id}")
    assert hidden.status_code == 404
    cancelled = await async_client.delete(f"/api/v1/printers/{printer_id}/files/uploads/{job_id}")
    assert cancelled.json()["phase"] == "cancelled"
    assert not staged[0].exists()
    assert not any(
        "file upload in progress" in row["reasons"] for row in current_registry().running_versions(sdk_plugin.id)
    )


@pytest.mark.parametrize("name,content,expected", [("../part.gcode", b"G1", 422), ("part.gcode", b"", 422)])
async def test_upload_rejects_invalid_files_without_dispatch(
    async_client, sdk_plugin, uploads, name, content, expected
):
    printer_id = await create_sdk_printer(async_client)
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload", data={"upload_id": uuid4().hex}, files={"file": (name, content)}
    )
    assert response.status_code == expected
    assert not uploads.jobs and not sdk_plugin.calls


async def test_upload_requires_declared_capability_and_bounded_size(async_client, sdk_plugin, uploads, monkeypatch):
    printer_id = await create_sdk_printer(async_client)
    monkeypatch.setattr(printer_uploads, "MAX_UPLOAD_BYTES", 2)
    oversized = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": uuid4().hex},
        files={"file": ("part.gcode", b"G1 X1")},
    )
    assert oversized.status_code == 413
    monkeypatch.setattr(sdk_plugin, "model_capabilities", lambda _model: frozenset({"print"}))
    unsupported = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": uuid4().hex},
        files={"file": ("part.gcode", b"G1")},
    )
    assert unsupported.status_code == 409
    assert not uploads.jobs and not sdk_plugin.calls


async def test_upload_limit_rejects_content_length_before_multipart_parsing(async_client, uploads, monkeypatch):
    from starlette.formparsers import MultiPartParser

    async def forbidden_parse(self):
        pytest.fail("Oversized request reached multipart parsing")

    monkeypatch.setattr(MultiPartParser, "parse", forbidden_parse)
    response = await async_client.post(
        "/api/v1/printers/1/files/upload",
        content=b"x",
        headers={"Content-Length": str(printer_uploads.MAX_UPLOAD_BYTES + 2 * 1024 * 1024)},
    )
    assert response.status_code == 413
    assert not uploads.jobs


async def test_chunked_upload_limit_applies_during_multipart_parsing(async_client, uploads, monkeypatch):
    monkeypatch.setattr(printer_uploads, "MAX_UPLOAD_BYTES", 4)
    payload = (
        b'--limit\r\nContent-Disposition: form-data; name="file"; filename="part.gcode"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n" + b"x" * (1024 * 1024 + 100) + b"\r\n--limit--\r\n"
    )

    async def chunks():
        for offset in range(0, len(payload), 65536):
            yield payload[offset : offset + 65536]

    response = await async_client.post(
        "/api/v1/printers/1/files/upload",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=limit"},
    )
    assert response.status_code == 413, response.text
    assert not uploads.jobs


async def test_cancellation_before_post_prevents_native_transfer_and_is_printer_scoped(
    async_client, sdk_plugin, uploads
):
    printer_id = await create_sdk_printer(async_client)
    job_id = uuid4().hex
    cancelled = await async_client.delete(f"/api/v1/printers/{printer_id}/files/uploads/{job_id}")
    assert cancelled.status_code == 200 and cancelled.json()["phase"] == "cancelled"
    hidden = await async_client.get(f"/api/v1/printers/{printer_id + 1}/files/uploads/{job_id}")
    assert hidden.status_code == 404
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": job_id},
        files={"file": ("part.gcode", b"G1 X10")},
    )
    assert response.status_code == 409
    assert len(uploads.jobs) == 1 and not sdk_plugin.calls
    assert not list((settings.base_dir / "tmp" / "printer-uploads").glob("*.upload"))


async def test_cancellation_during_staging_never_starts_native_transfer(async_client, sdk_plugin, uploads, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    printer_id = await create_sdk_printer(async_client)
    job_id = uuid4().hex
    staged, release = asyncio.Event(), asyncio.Event()
    refresh = AsyncSession.refresh

    async def pause_refresh(self, *args, **kwargs):
        staged.set()
        await release.wait()
        return await refresh(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "refresh", pause_refresh)
    request = asyncio.create_task(
        async_client.post(
            f"/api/v1/printers/{printer_id}/files/upload",
            data={"upload_id": job_id},
            files={"file": ("part.gcode", b"G1 X10")},
        )
    )
    await staged.wait()
    cancelled = await async_client.delete(f"/api/v1/printers/{printer_id}/files/uploads/{job_id}")
    assert cancelled.json()["phase"] == "cancelled"
    release.set()
    response = await request
    assert response.status_code == 409
    assert not sdk_plugin.calls and uploads.get(printer_id, job_id).task is None
    assert not list((settings.base_dir / "tmp" / "printer-uploads").glob("*.upload"))


async def test_disconnected_receive_never_publishes_native_upload(async_client, sdk_plugin, uploads, monkeypatch):
    from starlette.requests import Request

    printer_id = await create_sdk_printer(async_client)

    async def disconnected(self):
        return True

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    job_id = uuid4().hex
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": job_id},
        files={"file": ("part.gcode", b"G1 X10")},
    )
    assert response.status_code == 409
    assert uploads.get(printer_id, job_id).phase == "cancelled"
    assert not sdk_plugin.calls


async def test_duplicate_id_never_resends_and_known_id_recovers_accepted_status(async_client, sdk_plugin, uploads):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def upload(self, config, operation, *args, **kwargs):
        calls.append(operation)
        entered.set()
        await release.wait()
        return True

    sdk_plugin.file_operation_for = MethodType(upload, sdk_plugin)
    printer_id = await create_sdk_printer(async_client)
    job_id = uuid4().hex

    async def submit():
        return await async_client.post(
            f"/api/v1/printers/{printer_id}/files/upload",
            data={"upload_id": job_id},
            files={"file": ("part.gcode", b"G1 X10")},
        )

    response = await submit()
    assert response.status_code == 202 and response.json()["id"] == job_id
    await entered.wait()
    recovered = await async_client.get(f"/api/v1/printers/{printer_id}/files/uploads/{job_id}")
    assert recovered.json()["phase"] == "uploading"
    assert (await submit()).status_code == 409
    release.set()
    await uploads.get(printer_id, job_id).task
    assert (await submit()).status_code == 409
    assert calls == ["upload_file_async"] and sdk_plugin.calls == []


async def test_cancellation_retains_bytes_and_provider_until_native_unwind_finishes(async_client, sdk_plugin, uploads):
    entered, unwinding, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    staged = []

    async def upload(self, config, operation, *args, **kwargs):
        staged.append(args[2])
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwinding.set()
            await release.wait()
            raise

    sdk_plugin.file_operation_for = MethodType(upload, sdk_plugin)
    printer_id = await create_sdk_printer(async_client)
    job_id = uuid4().hex
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": job_id},
        files={"file": ("part.gcode", b"G1 X10")},
    )
    assert response.status_code == 202
    await entered.wait()
    cancelled = asyncio.create_task(async_client.delete(f"/api/v1/printers/{printer_id}/files/uploads/{job_id}"))
    await unwinding.wait()
    assert uploads.get(printer_id, job_id).phase == "cancelling" and staged[0].exists()
    assert not cancelled.done()
    assert any(
        "file upload in progress" in row["reasons"] for row in current_registry().running_versions(sdk_plugin.id)
    )
    again = asyncio.create_task(async_client.delete(f"/api/v1/printers/{printer_id}/files/uploads/{job_id}"))
    await asyncio.sleep(0)
    release.set()
    assert (await cancelled).json()["phase"] == (await again).json()["phase"] == "cancelled"
    assert not staged[0].exists()


async def test_upload_history_is_bounded_without_evicting_active_receives(uploads):
    uploads.max_history = 2
    receiving = uploads.reserve(1, uuid4().hex, "part.gcode", 100)
    for _ in range(10):
        await uploads.cancel(2, uuid4().hex)
    assert len(uploads.jobs) == 2
    assert uploads.get(1, receiving.id) is receiving and receiving.phase == "receiving"


@pytest.mark.parametrize("upload_id", ["", "../escape", "A" * 32, "a" * 31, "a" * 33])
async def test_upload_ids_are_validated(async_client, sdk_plugin, uploads, upload_id):
    printer_id = await create_sdk_printer(async_client)
    response = await async_client.post(
        f"/api/v1/printers/{printer_id}/files/upload",
        data={"upload_id": upload_id},
        files={"file": ("part.gcode", b"G1 X10")},
    )
    assert response.status_code == 422 and not uploads.jobs and not sdk_plugin.calls

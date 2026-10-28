"""Native Anycubic cameras use the existing shared viewer/snapshot lifetime."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.routes import camera
from backend.app.models.printer import Printer
from backend.app.services import camera_fanout
from backend.app.services.anycubic import AnycubicMQTTClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
JPEG = b"\xff\xd8frame\xff\xd9"


@pytest.fixture
async def lan_camera(async_client, db_session, monkeypatch):
    row = Printer(
        name="S1",
        printer_type="anycubic",
        model="Kobra S1",
        serial_number="LAN-CAMERA",
        ip_address="192.168.1.151",
        access_code="",
        tls_fingerprint="a" * 64,
        external_camera_enabled=False,
    )
    db_session.add(row)
    await db_session.commit()
    client = AnycubicMQTTClient(row.ip_address, row.serial_number, model=row.model, tls_fingerprint="a" * 64)
    client.state.connected = True
    opened, closed = [], []

    async def generate(actual_client, _url, kind, fps, *, on_process=None, on_frame=None, stop_event=None):
        opened.append((actual_client, kind))
        try:
            while not stop_event.is_set():
                on_frame(JPEG)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"
                await asyncio.sleep(0.01)
        finally:
            closed.append(True)

    monkeypatch.setattr(camera_fanout, "_GRACE_SECONDS", 0.02)
    with (
        patch.object(camera.printer_manager, "get_client", return_value=client),
        patch.object(camera, "generate_anycubic_stream", generate),
    ):
        yield row.id, client, opened, closed
        await camera.forget_printer(row.id)


def request():
    return MagicMock(is_disconnected=AsyncMock(return_value=False))


async def open_camera(printer_id):
    response = await camera.camera_stream(printer_id, request(), fps=5, opener=None, viewer=None)
    stream = response.body_iterator
    assert JPEG in await asyncio.wait_for(anext(stream), 2)
    return stream


async def test_native_viewers_and_snapshot_share_one_upstream(lan_camera):
    printer_id, client, opened, closed = lan_camera
    first = await open_camera(printer_id)
    second = await open_camera(printer_id)
    snapshot = await camera.camera_snapshot(printer_id, None)
    assert snapshot.body == JPEG
    assert opened == [(client, "anycubic")]
    await first.aclose()
    assert JPEG in await asyncio.wait_for(anext(second), 2)
    assert not closed
    await second.aclose()
    await asyncio.sleep(0.1)
    assert closed == [True]
    assert printer_id not in camera._external_live_readers


async def test_native_snapshot_can_start_and_release_camera_without_viewers(lan_camera):
    printer_id, client, opened, closed = lan_camera
    snapshot = await camera.camera_snapshot(printer_id, None)
    assert snapshot.body == JPEG
    assert opened == [(client, "anycubic")]
    await asyncio.sleep(0.1)
    assert closed == [True]
    assert printer_id not in camera._last_frames


@pytest.mark.parametrize("endpoint", ["stream", "snapshot"])
async def test_disconnected_native_camera_returns_service_unavailable(lan_camera, async_client, endpoint):
    printer_id, client, opened, _ = lan_camera
    client.state.connected = False
    response = await async_client.get(f"/api/v1/printers/{printer_id}/camera/{endpoint}")
    assert response.status_code == 503
    assert opened == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("ip_address", "192.168.1.152"),
        ("serial_number", "another-printer"),
        ("model", "Kobra X"),
        ("tls_fingerprint", "b" * 64),
    ],
)
async def test_edited_identity_cannot_reuse_the_previous_connection(lan_camera, async_client, db_session, field, value):
    printer_id, _, opened, _ = lan_camera
    row = await db_session.get(Printer, printer_id)
    setattr(row, field, value)
    await db_session.commit()
    response = await async_client.get(f"/api/v1/printers/{printer_id}/camera/stream")
    assert response.status_code == 503
    assert opened == []

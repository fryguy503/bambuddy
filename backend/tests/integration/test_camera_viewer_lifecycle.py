"""Renewing MJPEG must release the prior browser connection, for every camera."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.api.routes import camera
from backend.app.core.auth import StreamOpener
from backend.app.models.api_key import APIKey
from backend.app.models.printer import Printer
from backend.app.services import camera_fanout, external_camera
from backend.app.services.anycubic import AnycubicMQTTClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(params=["native", "external", "bambu"])
async def live_camera(request, async_client, db_session, monkeypatch):
    kind = request.param
    row = Printer(
        name="Lifecycle camera",
        serial_number="CAM-LIFECYCLE",
        ip_address="192.0.2.71",
        access_code="x",
        model="Kobra S1" if kind == "native" else "P1S",
        printer_type="anycubic" if kind == "native" else "bambu",
        tls_fingerprint="a" * 64 if kind == "native" else None,
        external_camera_enabled=kind == "external",
        external_camera_url="http://192.0.2.72/mjpeg" if kind == "external" else None,
        external_camera_type="mjpeg" if kind == "external" else None,
    )
    db_session.add(row)
    await db_session.commit()
    client = MagicMock(spec=AnycubicMQTTClient)
    client.state = SimpleNamespace(connected=True)
    client.is_stale = False
    for field in ("ip_address", "serial_number", "model", "tls_fingerprint"):
        setattr(client, field, getattr(row, field))
    monkeypatch.setattr(camera.printer_manager, "get_client", lambda _id: client)
    opened = []

    async def frames(*_args, on_frame=None, stop_event=None, disconnect_event=None, **_kwargs):
        opened.append(kind)
        stop = stop_event or disconnect_event
        while stop is None or not stop.is_set():
            if on_frame is not None:
                on_frame(b"\xff\xd8frame\xff\xd9")
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8frame\xff\xd9\r\n"
            await asyncio.sleep(0.01)

    monkeypatch.setattr(camera_fanout, "_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(camera, "generate_anycubic_stream", frames)
    monkeypatch.setattr(external_camera, "generate_mjpeg_stream", frames)
    monkeypatch.setattr(camera, "generate_chamber_mjpeg_stream", frames)
    yield row.id, opened
    await camera.forget_printer(row.id)


async def open_viewer(printer_id, viewer, opener=None):
    request = MagicMock(is_disconnected=AsyncMock(return_value=False))
    response = await camera.camera_stream(printer_id, request, fps=5, opener=opener, viewer=viewer)
    stream = response.body_iterator
    assert await asyncio.wait_for(anext(stream), 2)
    return stream


async def ended(stream):
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), 0.5)


def subscribers(printer_id):
    return sum(camera_fanout.get_subscriber_count(key) for key in camera._printer_broadcaster_keys(printer_id))


async def test_renewal_and_targeted_stop_release_only_the_matching_viewer(live_camera):
    printer_id, opened = live_camera
    first = await open_viewer(printer_id, "tile-a")
    other = await open_viewer(printer_id, "tile-b")
    renewed = await open_viewer(printer_id, "tile-a")
    await ended(first)
    assert subscribers(printer_id) == 2
    assert len(opened) == 1
    await camera.stop_camera_stream(printer_id, _=None, viewer="tile-a", api_key=None)
    await ended(renewed)
    assert subscribers(printer_id) == 1
    assert await asyncio.wait_for(anext(other), 0.5)
    await other.aclose()
    assert subscribers(printer_id) == 0


async def test_same_viewer_string_cannot_stop_or_replace_another_users_stream(live_camera):
    printer_id, _ = live_camera
    alice = await open_viewer(printer_id, "embedded", StreamOpener(auth_on=True, username="alice"))
    bob = await open_viewer(printer_id, "embedded", StreamOpener(auth_on=True, username="bob"))
    await camera.stop_camera_stream(
        printer_id, _=SimpleNamespace(username="alice", id=10), viewer="embedded", api_key=None
    )
    await ended(alice)
    assert subscribers(printer_id) == 1
    assert await asyncio.wait_for(anext(bob), 0.5)
    await bob.aclose()


async def test_a_departing_predecessor_cannot_remove_its_successors_registration(live_camera):
    printer_id, _ = live_camera
    first = await open_viewer(printer_id, "popup")
    second = await open_viewer(printer_id, "popup")
    await ended(first)
    key = (printer_id, ("anonymous",), "popup")
    assert key in camera._viewer_streams
    third = await open_viewer(printer_id, "popup")
    await ended(second)
    assert key in camera._viewer_streams
    await camera.stop_camera_stream(printer_id, _=None, viewer="popup", api_key=None)
    await ended(third)
    assert key not in camera._viewer_streams


async def test_stop_cancels_a_waiting_frame_instead_of_waiting_for_camera_recovery():
    waiting = asyncio.Event()
    closed = asyncio.Event()

    async def frames():
        try:
            yield b"first"
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    stream = camera._viewer_frames(frames(), 123456, None, "blocked")
    assert await anext(stream) == b"first"
    pending = asyncio.create_task(anext(stream))
    await waiting.wait()
    await camera.stop_camera_stream(123456, _=None, viewer="blocked", api_key=None)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, 0.5)
    assert closed.is_set()
    assert (123456, ("anonymous",), "blocked") not in camera._viewer_streams


async def test_api_key_owner_and_long_lived_user_owner_are_resolved_without_tokens():
    first_key = APIKey(id=17, key_prefix="public-prefix-a")
    other_key = APIKey(id=17, key_prefix="public-prefix-b")
    owner = camera._stream_viewer_owner(StreamOpener(auth_on=True, api_key_id=17, api_key_prefix="public-prefix-a"))
    assert owner in camera._stop_viewer_owners(None, first_key)
    assert owner not in camera._stop_viewer_owners(None, other_key)
    long_lived = camera._stream_viewer_owner(StreamOpener(auth_on=True, long_lived_id=9, long_lived_user_id=32))
    assert long_lived in camera._stop_viewer_owners(SimpleNamespace(username="owner", id=32), None)
    assert long_lived not in camera._stop_viewer_owners(SimpleNamespace(username="another", id=33), None)

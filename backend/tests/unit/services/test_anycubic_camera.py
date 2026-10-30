"""LAN camera boundaries, activation and decoder lifetime; no network access."""

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.anycubic import AnycubicError, AnycubicMQTTClient, camera
from backend.app.services.anycubic.protocol import camera_stream_url

HOST = "192.168.1.151"
JPEG = b"\xff\xd8picture\xff\xd9"


def ready_client(model="Kobra S1"):
    client = AnycubicMQTTClient(HOST, "camera-test", model=model, tls_fingerprint="a" * 64)
    client.state.connected = True
    client._last_message_monotonic = time.monotonic()
    return client


def test_camera_destination_is_always_the_configured_printer():
    assert camera_stream_url(HOST) == f"http://{HOST}:18088/flv"
    assert camera_stream_url(HOST, "http://other.example:18088/live/token?key=private") == (
        f"http://{HOST}:18088/live/token?key=private"
    )
    assert camera_stream_url("fd12::1") == "http://[fd12::1]:18088/flv"


@pytest.mark.parametrize(
    "url",
    [
        "https://printer:18088/flv",
        "http://printer:80/flv",
        "http://printer:18088/admin",
        "http://user:secret@printer:18088/flv",
        "http://printer:18088/flv#private",
        "http://printer:18088/live/token\n",
        "http://printer:18088/live/" + "a" * 2100,
        "http://printer:invalid/flv",
        "file:///flv",
    ],
)
def test_camera_refuses_other_protocols_ports_credentials_and_paths(url):
    with pytest.raises(AnycubicError) as error:
        camera_stream_url(HOST, url)
    assert url not in str(error.value)


def test_s1_activation_is_camera_only_and_reuses_recent_session():
    client = ready_client()

    def publish(kind, action, data):
        if action == "startCapture":
            client._camera_report("video", {"state": "initSuccess", "code": 200})
        return True

    client._publish = MagicMock(side_effect=publish)
    assert client.start_camera() == f"http://{HOST}:18088/flv"
    assert client.start_camera() == f"http://{HOST}:18088/flv"
    assert [call.args for call in client._publish.call_args_list] == [
        ("video", "startCapture", None),
        ("info", "query", None),
    ]


def test_new_camera_firmware_waits_for_token_url_after_stop_ack():
    client = ready_client("Kobra X")

    def publish(kind, action, data):
        if action == "stopCapture":
            client._camera_report(
                "video",
                {
                    "state": "pushStopped",
                    "code": 200,
                    "data": {"urls": {"rtspUrl": "http://printer:18088/live/old-token"}},
                },
            )
        elif action == "startCapture":
            client._camera_report("video", {"state": "initSuccess", "code": 200})
        elif kind == "info":
            client._camera_report("info", {"data": {"urls": {"rtspUrl": "http://printer:18088/live/private"}}})
        return True

    client._publish = MagicMock(side_effect=publish)
    with patch.object(client._stop, "wait", return_value=False):
        assert client.start_camera() == f"http://{HOST}:18088/live/private"
    assert [call.args[:2] for call in client._publish.call_args_list] == [
        ("video", "stopCapture"),
        ("video", "startCapture"),
        ("info", "query"),
    ]


@pytest.mark.parametrize("failure", ["offline", "stale", "rejected", "no_ack", "disconnected"])
def test_unavailable_camera_never_returns_an_old_or_unconfirmed_url(failure):
    client = ready_client()

    def publish(kind, action, data):
        if failure == "rejected":
            client._camera_report("video", {"code": 500, "state": "initFailed"})
        elif failure == "disconnected":
            client._generation += 1
            client._reset_camera()
        return True

    client._publish = MagicMock(side_effect=publish)
    if failure == "offline":
        client.state.connected = False
    elif failure == "stale":
        client._last_message_monotonic -= 120
    with pytest.raises(AnycubicError):
        client.start_camera(timeout=0.01)


class Content:
    def __init__(self, signature=b"FLV"):
        self.signature = signature

    async def readexactly(self, _count):
        return self.signature

    async def iter_chunked(self, _size):
        yield b"video-bytes"
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr_error", [False, True])
async def test_decoder_uses_private_stdin_and_is_reaped_when_viewer_leaves(stderr_error):
    client = ready_client()
    client.start_camera = MagicMock(return_value=f"http://{HOST}:18088/live/secret-token")
    content = Content()
    response = MagicMock(status=200, content=content)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock()
    session = MagicMock(get=AsyncMock(return_value=response))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    output = asyncio.StreamReader()
    output.feed_data(JPEG[:5])
    output.feed_data(JPEG[5:])
    error = AsyncMock(read=AsyncMock(side_effect=OSError("pipe closed"))) if stderr_error else asyncio.StreamReader()
    process = SimpleNamespace(stdin=MagicMock(drain=AsyncMock()), stdout=output, stderr=error)
    on_process, on_frame = MagicMock(), MagicMock()
    with (
        patch.object(camera, "get_ffmpeg_path", return_value="ffmpeg"),
        patch.object(camera.aiohttp, "ClientSession", return_value=session) as make_session,
        patch.object(camera.asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=process) as spawn,
        patch.object(camera, "_end_stream_ffmpeg", new_callable=AsyncMock) as reap,
    ):
        async with contextlib.aclosing(
            camera.generate_mjpeg_stream(
                client,
                "public-source-key",
                "anycubic",
                on_process=on_process,
                on_frame=on_frame,
            )
        ) as frames:
            frame = await asyncio.wait_for(anext(frames), 1)
            await asyncio.sleep(0)
        assert JPEG in frame
        on_frame.assert_called_once_with(JPEG)
        on_process.assert_called_once_with(process)
        reap.assert_awaited_once_with(process)
        process.stdin.close.assert_called_once()
        assert make_session.call_args.kwargs["trust_env"] is False
        assert session.get.call_args.kwargs["allow_redirects"] is False
        assert "secret-token" not in repr(spawn.call_args)
        assert spawn.call_args.args[spawn.call_args.args.index("-protocol_whitelist") + 1] == "pipe"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,signature", [(302, b"FLV"), (200, b"bad")])
async def test_redirect_or_non_flv_response_never_starts_decoder(status, signature, caplog):
    client = ready_client()
    client.start_camera = MagicMock(return_value=f"http://{HOST}:18088/live/secret-token")
    response = MagicMock(status=status, content=Content(signature))
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock()
    session = MagicMock(get=AsyncMock(return_value=response))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    with (
        patch.object(camera, "get_ffmpeg_path", return_value="ffmpeg"),
        patch.object(camera.aiohttp, "ClientSession", return_value=session),
        patch.object(camera.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn,
    ):
        assert [chunk async for chunk in camera.generate_mjpeg_stream(client, "", "")] == []
        spawn.assert_not_called()
    assert "secret-token" not in caplog.text

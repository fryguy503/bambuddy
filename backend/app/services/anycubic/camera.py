"""Private LAN HTTP-FLV decoding for Anycubic cameras; no browser/vendor relay."""

import asyncio
import contextlib
import logging

import aiohttp

from backend.app.services.camera import get_ffmpeg_path
from backend.app.services.external_camera import _end_stream_ffmpeg

from .protocol import AnycubicError, camera_stream_url

logger = logging.getLogger(__name__)
MAX_JPEG_BYTES = 8 * 1024 * 1024


async def generate_mjpeg_stream(client, _source, _kind, fps=10, *, on_process=None, on_frame=None, stop_event=None):
    """Adapt one camera session to the existing shared MJPEG upstream interface.

    HTTP redirects and environment proxies are disabled. ffmpeg receives bytes
    through stdin, so it sees neither a bearer URL nor any network protocol.
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise AnycubicError("ffmpeg is required for the Anycubic camera")
    url = camera_stream_url(client.ip_address, await asyncio.to_thread(client.start_camera))
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=15)
    process = None
    pump = None
    stderr = None
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            response = None
            for attempt in range(4):
                if stop_event is not None and stop_event.is_set():
                    return
                try:
                    candidate = await session.get(url, allow_redirects=False)
                    if candidate.status == 200:
                        response = candidate
                        break
                    candidate.close()
                    if 300 <= candidate.status < 400:
                        raise AnycubicError("Anycubic camera redirects are not supported")
                except (aiohttp.ClientError, TimeoutError):
                    pass
                if attempt < 3:
                    await asyncio.sleep(1)
            if response is None:
                raise AnycubicError("The Anycubic LAN camera is not responding")
            async with response:
                signature = await response.content.readexactly(3)
                if signature != b"FLV":
                    raise AnycubicError("The printer did not provide an HTTP-FLV camera stream")
                process = await asyncio.create_subprocess_exec(
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-protocol_whitelist",
                    "pipe",
                    "-f",
                    "flv",
                    "-i",
                    "pipe:0",
                    "-an",
                    "-vf",
                    f"fps={max(1, min(int(fps), 15))}",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "-q:v",
                    "5",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                if on_process is not None:
                    on_process(process)
                input_stream, output_stream, error_stream = process.stdin, process.stdout, process.stderr
                if input_stream is None or output_stream is None or error_stream is None:
                    raise AnycubicError("Anycubic camera decoder pipes are unavailable")

                async def feed():
                    try:
                        input_stream.write(signature)
                        async for chunk in response.content.iter_chunked(65536):
                            if stop_event is not None and stop_event.is_set():
                                break
                            input_stream.write(chunk)
                            await input_stream.drain()
                    except (aiohttp.ClientError, OSError, TimeoutError):
                        pass
                    finally:
                        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                            input_stream.close()

                async def drain_stderr():
                    while await error_stream.read(8192):
                        pass  # Stream metadata and session tokens must not enter logs.

                pump = asyncio.create_task(feed())
                stderr = asyncio.create_task(drain_stderr())
                buffer = b""
                while stop_event is None or not stop_event.is_set():
                    chunk = await asyncio.wait_for(output_stream.read(65536), timeout=20)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > MAX_JPEG_BYTES:
                        raise AnycubicError("Anycubic camera frame exceeds its size limit")
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        if start < 0:
                            buffer = buffer[-1:]
                            break
                        end = buffer.find(b"\xff\xd9", start + 2)
                        if end < 0:
                            buffer = buffer[start:]
                            break
                        frame, buffer = buffer[start : end + 2], buffer[end + 2 :]
                        if on_frame is not None:
                            on_frame(frame)
                        yield (
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(frame)).encode()
                            + b"\r\n\r\n"
                            + frame
                            + b"\r\n"
                        )
    except (aiohttp.ClientError, OSError, TimeoutError, asyncio.IncompleteReadError, AnycubicError):
        logger.warning("Anycubic LAN camera stream could not be opened or ended")
    finally:
        tasks = [task for task in (pump, stderr) if task is not None]
        for task in tasks:
            task.cancel()
        try:
            # A pipe may already have failed while /camera/stop was reaping
            # the decoder. Retrieve task exceptions without skipping cleanup.
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if process is not None:
                await _end_stream_ffmpeg(process)

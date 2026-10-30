"""Bambu live camera protocol generators.

``runtime`` supplies the host's stream bookkeeping and process lifecycle hooks.
This lets the bundled adapter share Bambuddy's fan-out, stop, and cleanup paths
without putting proprietary transport selection in the HTTP endpoints.
"""

import asyncio
import subprocess
import sys
import time
import zlib
from collections.abc import AsyncGenerator


async def generate_chamber_mjpeg_stream(
    ip_address: str,
    access_code: str,
    model: str | None,
    fps: int = 5,
    stream_id: str | None = None,
    disconnect_event: asyncio.Event | None = None,
    printer_id: int | None = None,
    *,
    runtime,
) -> AsyncGenerator[bytes, None]:
    """Generate MJPEG stream from A1/P1 printer using chamber image protocol.

    This connects to port 6000 and reads JPEG frames using the Bambu binary protocol.
    """
    runtime.logger.info("Starting chamber image stream for %s (stream_id=%s, model=%s)", ip_address, stream_id, model)

    # Register disconnect event so stop endpoint can signal us
    if stream_id and disconnect_event:
        runtime._disconnect_events[stream_id] = disconnect_event

    connection = await runtime.generate_chamber_image_stream(ip_address, access_code, fps)
    if connection is None:
        runtime.logger.error("Failed to connect to chamber image stream for %s", ip_address)
        yield (
            b"--frame\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Error: Camera connection failed. Check printer is on and camera is enabled.\r\n"
        )
        return

    reader, writer = connection

    # Track active connection for cleanup
    if stream_id:
        runtime._active_chamber_streams[stream_id] = (reader, writer)

    try:
        frame_interval = 1.0 / fps if fps > 0 else 0.2
        last_frame_time = 0.0

        while True:
            # Check if client disconnected
            if disconnect_event and disconnect_event.is_set():
                runtime.logger.info("Client disconnected, stopping chamber stream %s", stream_id)
                break

            # Read next frame
            frame = await runtime.read_next_chamber_frame(reader, timeout=30.0)
            if frame is None:
                runtime.logger.warning("Chamber image stream ended for %s", stream_id)
                break

            # Save frame to buffer for photo capture and track timestamp
            if printer_id is not None:
                runtime._last_frames[printer_id] = frame
                runtime._last_frame_times[printer_id] = time.time()

            # Rate limiting - skip frames if needed to maintain target FPS
            current_time = asyncio.get_event_loop().time()
            if current_time - last_frame_time < frame_interval:
                continue
            last_frame_time = current_time

            # Yield frame in MJPEG format
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                b"\r\n" + frame + b"\r\n"
            )

    except asyncio.CancelledError:
        runtime.logger.info("Chamber image stream cancelled (stream_id=%s)", stream_id)
    except GeneratorExit:
        runtime.logger.info("Chamber image stream generator exit (stream_id=%s)", stream_id)
    except Exception as e:
        runtime.logger.exception("Chamber image stream error: %s", e)
    finally:
        # Remove from active streams and disconnect events
        if stream_id:
            runtime._active_chamber_streams.pop(stream_id, None)
            runtime._disconnect_events.pop(stream_id, None)
            runtime._stream_last_frame_times.pop(stream_id, None)

        # Clean up frame buffer and timestamps
        runtime._release_printer_frame_state(printer_id)

        # Close the connection
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass  # Connection already closed or broken; cleanup is best-effort
        runtime.logger.info("Chamber image stream stopped for %s (stream_id=%s)", ip_address, stream_id)


async def generate_rtsp_mjpeg_stream(
    ip_address: str,
    access_code: str,
    model: str | None,
    fps: int = 10,
    stream_id: str | None = None,
    disconnect_event: asyncio.Event | None = None,
    printer_id: int | None = None,
    *,
    runtime,
) -> AsyncGenerator[bytes, None]:
    """Generate MJPEG stream from printer camera using ffmpeg/RTSP.

    This is for X1/H2/P2 models that support RTSP streaming.
    Auto-reconnects when the printer drops the RTSP session (common on P2S).
    Per-model knobs (probesize, analyzeduration, reconnect cadence) come from
    :func:`camera_profiles.get_camera_profile` so quirky firmwares can be
    handled by adding a profile entry rather than tuning a global constant.
    """
    ffmpeg = runtime.get_ffmpeg_path()
    if not ffmpeg:
        runtime.logger.error("ffmpeg not found - camera streaming requires ffmpeg")
        yield (b"--frame\r\nContent-Type: text/plain\r\n\r\nError: ffmpeg not installed\r\n")
        return

    profile = runtime.get_camera_profile(model)

    port = runtime.get_camera_port(model)

    # Use a local TLS proxy so Python's OpenSSL handles TLS instead of
    # ffmpeg's GnuTLS.  This fixes P2S (and potentially other models)
    # dropping the RTSP session after a few seconds due to GnuTLS's
    # hardened Debian defaults rejecting TLS renegotiation.
    proxy_port, proxy_server = await runtime.create_tls_proxy(ip_address, port)
    camera_url = f"rtsp://bblp:{access_code}@127.0.0.1:{proxy_port}/streaming/live/1"

    # ffmpeg command to output MJPEG stream to stdout
    cmd = [
        ffmpeg,
        *runtime.FFMPEG_RTSP_LOGLEVEL,
        "-rtsp_transport",
        "tcp",
        "-rtsp_flags",
        "prefer_tcp",
        # Socket I/O timeout name varies by ffmpeg version (#1504); see
        # rtsp_socket_timeout_flag(). The 30s value is microseconds for
        # both names.
        f"-{runtime.rtsp_socket_timeout_flag()}",
        "30000000",
        "-buffer_size",
        "1024000",  # 1MB buffer
        "-max_delay",
        "500000",  # 0.5 seconds max delay
        "-probesize",
        str(profile.probesize),
        "-analyzeduration",
        str(profile.analyzeduration),
        "-fflags",
        "nobuffer",  # Reduce internal buffering
        "-flags",
        "low_delay",  # Minimize decode latency
        *profile.extra_ffmpeg_input_args,
        "-i",
        camera_url,
        "-f",
        "mjpeg",
        "-q:v",
        "5",
        "-r",
        str(fps),
        "-an",  # No audio
        "-",  # Output to stdout
    ]

    # Register disconnect event so stop endpoint can signal us
    if stream_id and disconnect_event:
        runtime._disconnect_events[stream_id] = disconnect_event

    runtime.logger.info(
        "Starting RTSP camera stream for %s (stream_id=%s, model=%s, fps=%s, probesize=%s, analyzeduration=%s)",
        ip_address,
        stream_id,
        model,
        fps,
        profile.probesize,
        profile.analyzeduration,
    )
    # Log the full argv so a support bundle shows the actual ffmpeg flags
    # (probesize, analyzeduration, transport, ...). Only camera_url carries a
    # secret (the access code), so redact just that one element.
    _redacted_cmd = ["rtsp://<redacted>/streaming/live/1" if a == camera_url else a for a in cmd]
    runtime.logger.debug("ffmpeg command: %s", " ".join(_redacted_cmd))

    # On Windows, spawn ffmpeg in its own process group so that
    # terminate() doesn't broadcast CTRL_C_EVENT to uvicorn (#605).
    spawn_kwargs: dict = {}
    if sys.platform == "win32":
        spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    jpeg_start = b"\xff\xd8"
    jpeg_end = b"\xff\xd9"
    reconnect_count = 0
    process = None
    stderr_tail = None
    got_any_frames = False
    # Across sessions (#3218): the frame a session froze on, and whether the
    # next session showed the very same picture -- a still scene, not a
    # frozen ffmpeg.
    frozen_crc: int | None = None
    still_scene = False

    try:
        while reconnect_count <= profile.rtsp_reconnect_max:
            # Check for client disconnect before (re)connecting
            if disconnect_event and disconnect_event.is_set():
                break

            if reconnect_count > 0:
                runtime.logger.info(
                    "RTSP reconnecting (%d/%d) for %s (stream_id=%s)",
                    reconnect_count,
                    profile.rtsp_reconnect_max,
                    ip_address,
                    stream_id,
                )
                # Fast after a session that delivered frames (reconnect_count
                # is 1 then), backing off while the printer keeps refusing.
                await asyncio.sleep(
                    min(
                        profile.rtsp_reconnect_delay * 2 ** (reconnect_count - 1),
                        profile.rtsp_reconnect_backoff_max,
                    )
                )
                if disconnect_event and disconnect_event.is_set():
                    break

            # Spawn ffmpeg
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_kwargs,
            )

            if stream_id:
                runtime._active_streams[stream_id] = process
            import time as _time

            runtime._spawned_ffmpeg_pids[process.pid] = _time.time()

            # Brief check for immediate startup failures
            await asyncio.sleep(0.1)
            if process.returncode is not None:
                stderr = await process.stderr.read()
                stderr_text = runtime._summarize_ffmpeg_stderr(stderr.decode(errors="replace"))
                runtime.logger.error("ffmpeg failed immediately (attempt %d): %s", reconnect_count + 1, stderr_text)
                runtime._spawned_ffmpeg_pids.pop(process.pid, None)
                if not got_any_frames and reconnect_count == 0:
                    # First attempt failed immediately — camera is likely unreachable
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: text/plain\r\n\r\n"
                        b"Error: Camera connection failed. Check printer is on and camera is enabled.\r\n"
                    )
                    return
                reconnect_count += 1
                continue

            # Take ownership of stderr for the life of this process. Started
            # only after the immediate-failure check above, which reads the pipe
            # directly (correct there: the process is already dead, so
            # read-to-EOF returns at once and cannot be raced by a collector).
            # Nothing is lost by starting late — the banner ffmpeg printed in the
            # meantime is still sitting in the pipe.
            stderr_tail = runtime._FfmpegStderrTail(process)

            # Read JPEG frames from ffmpeg stdout
            buffer = b""
            stream_ended = False
            client_gone = False
            session_got_frames = False
            # A frame that differs from the one before, not any frame, is what
            # shows the picture is live: ffmpeg can repeat its last one
            # forever (#3218).
            last_frame_crc: int | None = None
            last_change = time.time()
            identical_frames = 0
            frozen = False
            refused = False

            while True:
                if disconnect_event and disconnect_event.is_set():
                    client_gone = True
                    break

                try:
                    chunk = await asyncio.wait_for(process.stdout.read(8192), timeout=30.0)

                    if not chunk:
                        # ffmpeg exited — log stderr and break to reconnect
                        stderr_text = await runtime._read_ffmpeg_stderr(process)
                        if stderr_text:
                            runtime.logger.warning("ffmpeg stderr (stream_id=%s): %s", stream_id, stderr_text)
                        runtime.logger.warning(
                            "RTSP stream ended for %s (stream_id=%s), will reconnect", ip_address, stream_id
                        )
                        stream_ended = True
                        break

                    buffer += chunk

                    # Extract complete JPEG frames from buffer
                    while True:
                        start_idx = buffer.find(jpeg_start)
                        if start_idx == -1:
                            buffer = buffer[-2:] if len(buffer) > 2 else buffer
                            break

                        if start_idx > 0:
                            buffer = buffer[start_idx:]

                        end_idx = buffer.find(jpeg_end, 2)
                        if end_idx == -1:
                            break

                        frame = buffer[: end_idx + 2]
                        buffer = buffer[end_idx + 2 :]
                        # No frame before ffmpeg reported its input open: a
                        # redirect it followed on the way ends the stream.
                        if not session_got_frames and (
                            stderr_tail is None
                            or not await stderr_tail.wait_input_open(runtime._FFMPEG_INPUT_OPEN_WAIT)
                        ):
                            refused = True
                            break
                        got_any_frames = True
                        session_got_frames = True

                        now = time.time()
                        frame_crc = zlib.crc32(frame)
                        if last_frame_crc is None:
                            # First frame of this session. The same frame the
                            # last session froze on means the camera really
                            # shows that picture (see _RTSP_STILL_RECHECK_SECONDS).
                            still_scene = frozen_crc is not None and frame_crc == frozen_crc
                            if still_scene:
                                runtime.logger.info(
                                    "RTSP picture unchanged after restart for %s (stream_id=%s): "
                                    "treating it as a still scene, re-checking every %.0fs",
                                    ip_address,
                                    stream_id,
                                    runtime._RTSP_STILL_RECHECK_SECONDS,
                                )
                            last_frame_crc = frame_crc
                            last_change = now
                        elif frame_crc != last_frame_crc:
                            last_frame_crc = frame_crc
                            last_change = now
                            identical_frames = 0
                            # The picture moves: back to the normal check.
                            still_scene = False
                            frozen_crc = None
                        else:
                            identical_frames += 1

                        if printer_id is not None:
                            runtime._last_frames[printer_id] = frame
                            runtime._last_frame_times[printer_id] = now
                            if stream_id:
                                runtime._stream_last_frame_times[stream_id] = now

                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                            b"\r\n" + frame + b"\r\n"
                        )

                        if now - last_change > (
                            runtime._RTSP_STILL_RECHECK_SECONDS if still_scene else runtime._RTSP_FROZEN_SECONDS
                        ):
                            frozen = True
                            frozen_crc = last_frame_crc
                            break

                    if refused:
                        runtime.logger.error(
                            "RTSP input not confirmed open for %s (stream_id=%s), stopping the stream",
                            ip_address,
                            stream_id,
                        )
                        break

                    if frozen:
                        if still_scene:
                            runtime.logger.info(
                                "RTSP still-scene re-check for %s (stream_id=%s), restarting ffmpeg",
                                ip_address,
                                stream_id,
                            )
                        else:
                            stderr_text = await runtime._read_ffmpeg_stderr(process)
                            if stderr_text:
                                runtime.logger.warning("ffmpeg stderr (stream_id=%s): %s", stream_id, stderr_text)
                            runtime.logger.warning(
                                "RTSP output frozen for %s (stream_id=%s): %d identical frames over %.0fs, "
                                "restarting ffmpeg",
                                ip_address,
                                stream_id,
                                identical_frames,
                                time.time() - last_change,
                            )
                        stream_ended = True
                        break

                except TimeoutError:
                    stderr_text = await runtime._read_ffmpeg_stderr(process)
                    if stderr_text:
                        runtime.logger.warning("ffmpeg stderr on timeout: %s", stderr_text)
                    runtime.logger.warning("RTSP read timeout for %s (stream_id=%s)", ip_address, stream_id)
                    stream_ended = True
                    break
                except asyncio.CancelledError:
                    runtime.logger.info("Camera stream cancelled (stream_id=%s)", stream_id)
                    client_gone = True
                    break
                except GeneratorExit:
                    runtime.logger.info("Camera stream generator exit (stream_id=%s)", stream_id)
                    client_gone = True
                    break

            # A session that ended before its first frame may have ended on a
            # redirect (ffmpeg is killed for one): that is not redialled.
            if not session_got_frames and not client_gone and stderr_tail is not None:
                if stderr_tail.input_state is None:
                    await stderr_tail.wait_input_open(runtime._FFMPEG_REDIRECT_SETTLE)
                refused = refused or stderr_tail.input_state == "redirected"

            # Clean up this ffmpeg process before reconnecting or exiting
            await runtime._terminate_ffmpeg(process, stream_id)
            # Released after teardown, not before: _terminate_ffmpeg deliberately
            # leaves stderr to this collector, which has to keep draining while
            # the process is stopped or wait() can't observe the exit.
            if stderr_tail is not None:
                await stderr_tail.aclose()
                stderr_tail = None
            process = None

            if client_gone or refused:
                break

            # Check if stream was explicitly stopped (e.g., by stop endpoint)
            if stream_id and stream_id not in runtime._active_streams:
                runtime.logger.info("Stream %s removed from active streams, stopping reconnect", stream_id)
                break

            if stream_ended:
                # The budget is for failures in a row. A session that
                # delivered video was a success, however it ended -- a stock
                # X1C ends every session after about a minute, which under a
                # lifetime count stopped the live view for good after half an
                # hour.
                if session_got_frames:
                    reconnect_count = 0
                reconnect_count += 1
                continue

            # Normal exit (shouldn't reach here, but be safe)
            break

        if reconnect_count > profile.rtsp_reconnect_max:
            runtime.logger.error(
                "RTSP max consecutive reconnects (%d) reached for %s (stream_id=%s)",
                profile.rtsp_reconnect_max,
                ip_address,
                stream_id,
            )

    except FileNotFoundError:
        runtime.logger.error("ffmpeg not found - camera streaming requires ffmpeg")
        yield (b"--frame\r\nContent-Type: text/plain\r\n\r\nError: ffmpeg not installed\r\n")
    except asyncio.CancelledError:
        runtime.logger.info("Camera stream task cancelled (stream_id=%s)", stream_id)
    except GeneratorExit:
        runtime.logger.info("Camera stream generator closed (stream_id=%s)", stream_id)
    except Exception as e:
        runtime.logger.exception("Camera stream error: %s", e)
    finally:
        # Remove from active streams and disconnect events
        if stream_id:
            runtime._active_streams.pop(stream_id, None)
            runtime._disconnect_events.pop(stream_id, None)
            runtime._stream_last_frame_times.pop(stream_id, None)

        # Clean up frame buffer and timestamps
        runtime._release_printer_frame_state(printer_id)

        if process:
            await runtime._terminate_ffmpeg(process, stream_id)
            runtime.logger.info("Camera stream stopped for %s (stream_id=%s)", ip_address, stream_id)

        # Same order as in the loop: terminate first, then release stderr.
        if stderr_tail is not None:
            await stderr_tail.aclose()

        # Shut down the TLS proxy
        await runtime.close_tls_proxy(proxy_server)


def stream_fps(model: str | None, fps: int) -> int:
    from .camera import is_chamber_image_model

    return min(max(fps, 1), 5 if is_chamber_image_model(model) else 30)


def stream(*, model: str | None, runtime, **options):
    from .camera import is_chamber_image_model

    # The host retains these wrappers for existing in-process integrations.
    generate = (
        runtime.generate_chamber_mjpeg_stream if is_chamber_image_model(model) else runtime.generate_rtsp_mjpeg_stream
    )
    return generate(model=model, **options)

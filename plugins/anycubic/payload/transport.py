"""Native file and camera hooks used by the core's shared services."""

import asyncio
import contextlib
import os
import shutil
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from backend.app.core.config import settings
from backend.app.printers.contracts import UnsupportedPrinterOperation
from backend.app.printers.file_types import FileListResult, UploadCancelled

from .files import upload_gcode
from .printing import inspect_print_file
from .protocol import AnycubicError


async def file_operation(connection, operation, *args, **kwargs):
    native = connection.native
    if operation in {"list_files_async", "list_files_result_async", "find_remote_file_async"}:
        path = kwargs.get("path", kwargs.get("directory", args[2] if len(args) > 2 else "/"))
        if operation == "find_remote_file_async":
            path = "/"
        catalog = await asyncio.to_thread(
            native.list_files,
            path=path or "/",
            page=kwargs.get("page", 1),
            limit=kwargs.get("limit", 100),
            keyword=kwargs.get("keyword", ""),
        )
        rows = [{**row, "is_directory": row["is_dir"]} for row in catalog["files"]]
        if operation == "list_files_result_async":
            return FileListResult(files=rows, available=True)
        if operation == "find_remote_file_async":
            name = kwargs.get("filename", args[2] if len(args) > 2 else "")
            return next((row["path"] for row in rows if row["name"] == name), None)
        return rows
    if operation == "upload_file_async":
        local = Path(kwargs.get("local_path", args[2] if len(args) > 2 else ""))
        remote = kwargs.get("remote_path", args[3] if len(args) > 3 else local.name)
        alias = PurePosixPath(remote).name
        root = settings.base_dir / "printer-plugin-state" / "anycubic" / "artifacts"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = root / (uuid4().hex + ".gcode")
        await asyncio.to_thread(shutil.copyfile, local, staged)
        try:
            os.chmod(staged, 0o600)
            artifact = await asyncio.to_thread(inspect_print_file, staged, alias, connection.config.model)
            context = await asyncio.to_thread(native.file_session, require_upload=True)
            callback = kwargs.get("progress_callback")
            progress = (lambda sent: callback(sent, artifact.file.size)) if callback else None
            try:
                await upload_gcode(
                    native, context, artifact.file, progress=progress, cancelled=kwargs.get("cancel_event")
                )
                for attempt in range(3):
                    catalog = await asyncio.to_thread(
                        native.list_files, keyword=artifact.file.filename.rsplit("-", 1)[-1]
                    )
                    if any(
                        row["name"] == artifact.file.filename
                        and not row["is_dir"]
                        and row["size"] == artifact.file.size
                        for row in catalog["files"]
                    ):
                        break
                    if attempt == 2:
                        raise AnycubicError("The uploaded file identity was not confirmed; inspect the native catalog")
                    await asyncio.sleep(1)
            except (AnycubicError, OSError, TimeoutError) as exc:
                raise UploadCancelled(str(exc)) from exc
            record = {
                "path": str(staged),
                "source_filename": alias,
                "native_filename": artifact.file.filename,
                "sha256": artifact.file.sha256,
                "size": artifact.file.size,
                "model": artifact.file.model,
                "md5": artifact.md5,
                "estimates": artifact.estimates,
            }
            connection.journal.save_artifact(connection.printer_key, alias, record)
            connection.journal.save_artifact(connection.printer_key, remote, record)
            return True
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    if operation == "get_storage_info_async":
        return {"available": native.state.connected, "supports_download": False, "supports_delete": False}
    if operation == "remote_file_settled":
        return True
    raise UnsupportedPrinterOperation("The native Anycubic protocol does not provide " + operation)


def camera_operation(connection, operation, *args, **kwargs):
    if operation == "capture_in_flight":
        return connection.capture_lock.locked()
    if operation == "stream":
        return _stream(connection, fps=kwargs.get("fps", 10), stop_event=kwargs.get("disconnect_event"))
    if operation == "capture_camera_frame_bytes":
        return _capture(connection, timeout=kwargs.get("timeout", args[3] if len(args) > 3 else 15))
    if operation == "capture_camera_frame":
        return _capture_file(
            connection, Path(kwargs.get("output_path", args[3] if len(args) > 3 else "")), kwargs.get("timeout", 30)
        )
    if operation == "capture_finish_photo":
        output = Path(kwargs.get("archive_dir", args[4] if len(args) > 4 else "")) / (
            "finish_" + uuid4().hex[:12] + ".jpg"
        )
        return _finish(connection, output, kwargs.get("rotation", args[5] if len(args) > 5 else 0))
    if operation == "test_camera_connection":
        return _test_camera(connection)
    raise UnsupportedPrinterOperation(operation)


async def _stream(connection, *, fps=10, stop_event=None):
    from .camera import generate_mjpeg_stream

    async with (
        connection.capture_lock,
        contextlib.aclosing(
            generate_mjpeg_stream(
                connection.native,
                "",
                "flv",
                fps,
                stop_event=stop_event,
                on_frame=lambda frame: _remember(connection, frame),
            )
        ) as stream,
    ):
        async for chunk in stream:
            yield chunk


def _remember(connection, frame):
    connection.last_frame = (time.monotonic(), frame)


async def _capture(connection, timeout=15):
    cached = getattr(connection, "last_frame", None)
    if cached and time.monotonic() - cached[0] < 5:
        return cached[1]
    if connection.capture_lock.locked():
        return None
    from .camera import generate_mjpeg_stream

    frame = None

    def remember(value):
        nonlocal frame
        frame = value
        _remember(connection, value)

    try:
        async with (
            asyncio.timeout(timeout),
            connection.capture_lock,
            contextlib.aclosing(generate_mjpeg_stream(connection.native, "", "flv", 1, on_frame=remember)) as stream,
        ):
            await anext(stream)
            return frame
    except (TimeoutError, StopAsyncIteration):
        return None


async def _capture_file(connection, path, timeout=30):
    frame = await _capture(connection, timeout)
    if not frame:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, frame)
    return True


async def _finish(connection, path, rotation):
    if not await _capture_file(connection, path):
        return None
    if rotation:
        from PIL import Image

        def rotate():
            with Image.open(path) as image:
                image.rotate(-rotation, expand=True).save(path)

        await asyncio.to_thread(rotate)
    return path.name


async def _test_camera(connection):
    frame = await _capture(connection)
    return {"success": bool(frame), "message": "LAN camera frame received" if frame else "No LAN camera frame received"}

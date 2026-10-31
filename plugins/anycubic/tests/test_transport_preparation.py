"""Upload cancellation retains ownership until local worker I/O has ended."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.core.config import settings
from plugins.anycubic.payload import transport


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_fails", [False, True])
async def test_cancelled_copy_finishes_and_cleans_before_returning(monkeypatch, tmp_path, copy_fails):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    staged = []

    def copy(_source, destination):
        staged.append(Path(destination))
        entered.set()
        assert release.wait(5), "Test must release the copy worker"
        Path(destination).write_bytes(b"copied after cancellation")
        finished.set()
        if copy_fails:
            raise OSError("copy failed after cancellation")

    monkeypatch.setattr(transport.shutil, "copyfile", copy)
    native = MagicMock()
    connection = SimpleNamespace(native=native)
    task = asyncio.create_task(
        transport.file_operation(
            connection, "upload_file_async", local_path=tmp_path / "input.gcode", remote_path="input.gcode"
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        task.cancel()  # A repeated request must not release the worker early.
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert staged and not staged[0].exists()
        native.assert_not_called()
        assert native.method_calls == []
    finally:
        release.set()
        if not task.done():
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_failed_copy_removes_partial_artifact_without_native_request(monkeypatch, tmp_path):
    def failed_copy(_source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("synthetic copy failure")

    monkeypatch.setattr(transport.shutil, "copyfile", failed_copy)
    native = MagicMock()
    with pytest.raises(OSError, match="synthetic copy failure"):
        await transport.file_operation(
            SimpleNamespace(native=native), "upload_file_async", local_path=tmp_path / "input.gcode"
        )
    assert not list((settings.base_dir / "printer-plugin-state" / "anycubic" / "artifacts").iterdir())
    assert native.method_calls == []

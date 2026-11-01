"""Notification photos share addon camera arbitration with the live viewer."""

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app import main
from backend.app.api.routes import camera

pytestmark = pytest.mark.asyncio


@pytest.fixture
def snapshot_env(monkeypatch):
    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(main, "async_session", session)
    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true"))
    monkeypatch.setattr(main, "_apply_camera_rotation", lambda frame, *_args: frame)
    monkeypatch.setattr(camera, "_active_streams", {})
    monkeypatch.setattr(camera, "_active_chamber_streams", {})
    monkeypatch.setattr(camera, "_active_addon_streams", {})
    monkeypatch.setattr(camera, "_external_live_readers", {})
    monkeypatch.setattr(camera, "_last_frames", {})
    fresh = AsyncMock(return_value=b"fresh")
    monkeypatch.setattr("backend.app.printers.camera.capture_camera_frame_bytes", fresh)
    printer = SimpleNamespace(
        ip_address="192.0.2.7",
        access_code="x",
        model="Example printer",
        external_camera_enabled=False,
        external_camera_url=None,
    )
    return printer, fresh


@pytest.mark.parametrize("frame", [b"live frame", None, b"x" * 2_500_001])
async def test_notification_reuses_addon_live_view_or_skips_while_starting(snapshot_env, frame):
    printer, fresh = snapshot_env
    camera._active_addon_streams["addon-stream"] = 7
    if frame is not None:
        camera._last_frames[7] = frame

    result = await main._capture_snapshot_frame(7, printer, logging.getLogger())

    assert result == (frame if frame and len(frame) <= 2_500_000 else None)
    fresh.assert_not_awaited()


async def test_notification_without_live_view_captures_normally(snapshot_env):
    printer, fresh = snapshot_env

    assert await main._capture_snapshot_frame(7, printer, logging.getLogger()) == b"fresh"

    fresh.assert_awaited_once_with("192.0.2.7", "x", "Example printer", timeout=15, printer_id=7)


async def test_external_live_view_without_frame_does_not_fall_back_to_competing_capture(snapshot_env, monkeypatch):
    printer, fresh = snapshot_env
    printer.external_camera_enabled = True
    printer.external_camera_url = "http://192.0.2.8/camera"
    camera._external_live_readers[7] = 1
    external = AsyncMock(return_value=b"external")
    monkeypatch.setattr("backend.app.services.external_camera.capture_frame", external)

    assert await main._capture_snapshot_frame(7, printer, logging.getLogger()) is None

    external.assert_not_awaited()
    fresh.assert_not_awaited()

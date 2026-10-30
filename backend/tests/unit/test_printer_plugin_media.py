"""The existing camera, discovery and firmware workflows use the selected addon."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.app.printers import api, camera, diagnostic, firmware
from backend.app.printers.contracts import PrinterConfig, PrinterPlugin, UnsupportedPrinterOperation
from backend.app.printers.registry import PrinterPluginRegistry


class MediaAddon(PrinterPlugin):
    id = "media_fixture"
    name = "Media fixture"
    capabilities = frozenset({"camera", "discovery", "firmware"})

    def __init__(self):
        self.calls = []
        self.capture = AsyncMock(return_value=b"addon-frame")
        self.diagnose = AsyncMock(return_value="addon-diagnostic")
        self.check_service = SimpleNamespace(check_for_update=AsyncMock())
        self.update_service = object()
        self.upload_state = firmware.FirmwareUploadState(progress=37)
        self.discovery = SimpleNamespace(
            is_running=True,
            start=AsyncMock(),
            stop=AsyncMock(),
            discovered_printers=[],
        )
        self.scanner = SimpleNamespace(is_running=False, progress=(4, 8), discovered_printers=[])

    def create_connection(self, config, **callbacks):
        raise AssertionError("Media operations do not create a Bambu connection")

    def camera_operation(self, operation, *args, **kwargs):
        self.calls.append((operation, args, kwargs))
        if operation == "stream_fps":
            return 7
        if operation == "stream":
            return self.frames()
        return self.capture(*args, **kwargs)

    async def frames(self):
        yield b"addon-stream-frame"

    def discovery_operation(self, operation, *args, **kwargs):
        return {"service": self.discovery, "subnet_scanner": self.scanner}[operation]

    def firmware_operation(self, operation, *args, **kwargs):
        self.calls.append((operation, args, kwargs))
        return {
            "check_service": self.check_service,
            "service": self.update_service,
            "get_upload_state": self.upload_state,
            "upload_in_progress": True,
        }[operation]

    def diagnostic_operation(self, operation, *args, **kwargs):
        self.calls.append((operation, args, kwargs))
        return self.diagnose(*args, **kwargs)


@pytest.fixture
def media_addon(monkeypatch):
    from backend.app.printers import registry as registry_module

    registry = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(api, "_bindings", {})
    addon = MediaAddon()
    registry.register(addon)
    return addon


def _printer():
    return SimpleNamespace(
        id=8401,
        name="Addon printer",
        ip_address="192.0.2.84",
        serial_number="CaseSensitive",
        access_code="private",
        model="fixture",
        plugin_id="media_fixture",
        external_camera_enabled=False,
        external_camera_url=None,
    )


async def test_snapshot_uses_bound_addon_and_keeps_credentials_local(media_addon):
    printer = _printer()
    api.bind_printer(printer)

    assert (
        await camera.capture_camera_frame_bytes(printer.ip_address, printer.access_code, printer.model)
        == b"addon-frame"
    )
    assert media_addon.calls[0][0] == "capture_camera_frame_bytes"
    media_addon.capture.assert_awaited_once_with(printer.ip_address, printer.access_code, printer.model, 15)


async def test_explicit_addon_handles_unsaved_camera_test(media_addon):
    await camera.test_camera_connection("192.0.2.99", "private", "fixture", plugin_id=media_addon.id)
    assert media_addon.calls[0][0] == "test_camera_connection"


async def test_live_route_asks_addon_for_stream_without_bambu_protocol_selection(media_addon, monkeypatch):
    from backend.app.api.routes import camera as routes

    printer = _printer()

    @asynccontextmanager
    async def session():
        yield None

    async def broadcaster(_key, factory):
        import asyncio

        frames = factory(asyncio.Event())
        assert await anext(frames) == b"addon-stream-frame"
        await frames.aclose()
        return SimpleNamespace(subscribe=AsyncMock(return_value=None), subscriber_count=1)

    monkeypatch.setattr(routes.database, "async_session", session)
    monkeypatch.setattr(routes, "get_printer_or_404", AsyncMock(return_value=printer))
    monkeypatch.setattr(routes, "get_or_create_broadcaster", broadcaster)
    monkeypatch.setattr(routes, "_stream_start_times", {})
    response = await routes.camera_stream(printer.id, SimpleNamespace(), opener=None)

    assert response.status_code == 200
    assert [call[0] for call in media_addon.calls] == ["stream_fps", "stream"]
    assert media_addon.calls[1][2]["fps"] == 7


async def test_addon_jpeg_stream_uses_shared_frame_buffer_and_lifecycle(monkeypatch):
    import asyncio

    from backend.app.api.routes import camera as routes

    for name in (
        "_active_addon_streams",
        "_disconnect_events",
        "_last_frames",
        "_last_frame_times",
        "_stream_last_frame_times",
        "_stream_start_times",
    ):
        monkeypatch.setattr(routes, name, {})
    jpeg = b"\xff\xd8fixture-jpeg\xff\xd9"

    async def frames():
        yield jpeg

    upstream = routes._addon_upstream(frames(), 8401, "8401-addon-fixture", asyncio.Event())
    try:
        chunk = await anext(upstream)
        assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg")
        assert routes.get_buffered_frame(8401) == jpeg
        assert routes.is_stream_active(8401) is True
    finally:
        await upstream.aclose()
    assert routes.is_stream_active(8401) is False
    assert routes.get_buffered_frame(8401) is None


async def test_discovery_sessions_and_results_belong_to_selected_addon(media_addon):
    from backend.app.api.routes import discovery as routes

    media_addon.discovery.discovered_printers = [
        SimpleNamespace(serial="fixture", name="Fixture", ip_address="192.0.2.90", model="fixture", discovered_at=None)
    ]
    result = await routes.start_discovery(duration=2.0, plugin_id=media_addon.id)
    assert result.running is True
    media_addon.discovery.start.assert_awaited_once_with(duration=2.0)
    printers = await routes.get_discovered_printers(plugin_id=media_addon.id)
    assert len(printers) == 1
    assert printers[0].plugin_id == media_addon.id


def test_unknown_discovery_addon_never_falls_back_to_bambu(media_addon):
    from backend.app.api.routes.discovery import _discovery_backends

    with pytest.raises(HTTPException) as caught:
        _discovery_backends("not_installed")
    assert caught.value.status_code == 409


def test_firmware_services_and_upload_status_use_printer_addon(media_addon):
    printer = _printer()
    api.bind_printer(printer)

    assert firmware.get_firmware_service(printer) is media_addon.check_service
    assert firmware.get_firmware_update_service(printer) is media_addon.update_service
    assert firmware.get_upload_state(printer.id).progress == 37
    assert firmware.upload_in_progress(printer.id) is True


def test_addon_without_firmware_cannot_block_queue_idle_guard(media_addon):
    media_addon.capabilities = frozenset({"print"})
    printer = _printer()
    api.bind_printer(printer)

    assert firmware.upload_in_progress(printer.id) is False
    assert firmware.get_upload_state(printer.id).status is firmware.FirmwareUploadStatus.IDLE
    firmware.reset_upload_state(printer.id)
    assert media_addon.calls == []


async def test_saved_and_presave_diagnostics_choose_addon(media_addon):
    printer = _printer()
    assert await diagnostic.run_connection_diagnostic(printer.ip_address, printer=printer) == "addon-diagnostic"
    assert (
        await diagnostic.run_connection_diagnostic(printer.ip_address, plugin_id=media_addon.id) == "addon-diagnostic"
    )
    assert media_addon.diagnose.await_count == 2


async def test_optional_operations_fail_explicitly_without_vendor_fallback(media_addon):
    class MinimalAddon(PrinterPlugin):
        id = "minimal_fixture"
        name = "Minimal fixture"

        def create_connection(self, config, **callbacks):
            return None

    from backend.app.printers.registry import register_plugin

    register_plugin(MinimalAddon())
    api.bind_printer(PrinterConfig("192.0.2.85", "fixture", "private", plugin_id="minimal_fixture", printer_id=8402))
    with pytest.raises(UnsupportedPrinterOperation):
        await camera.capture_camera_frame_bytes("192.0.2.85", "private", "fixture")

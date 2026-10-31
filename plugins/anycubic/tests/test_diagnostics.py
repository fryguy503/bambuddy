"""Connection diagnostics exercise facts and cleanup without printer traffic."""

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import DiagnosticCheck
from plugins.anycubic.payload import diagnostic
from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

PIN = "a" * 64
HOST = "192.168.50.50"


@pytest.fixture
def probes(monkeypatch):
    info = {
        "ctrlType": "lan",
        "modelId": "20025",
        "modelName": "Kobra S1",
        "cn": "DIAG-IDENTITY",
        "token": "DO-NOT-EXPOSE-HTTP-HANDSHAKE-TOKEN-12345",
        "ctrlInfoUrl": f"http://{HOST}:18910/ctrl",
    }
    read = AsyncMock(return_value=info)
    tcp = AsyncMock(return_value=True)
    certificate = AsyncMock(return_value=PIN)
    advice = AsyncMock(return_value=[DiagnosticCheck(id="network_mode", status="pass")])
    monkeypatch.setattr(diagnostic, "_info", read)
    monkeypatch.setattr(diagnostic, "_tcp", tcp)
    monkeypatch.setattr(diagnostic, "_certificate", certificate)
    monkeypatch.setattr(diagnostic, "_network_advice", advice)
    return SimpleNamespace(info=info, read=read, tcp=tcp, certificate=certificate)


@pytest.fixture
def config():
    return PrinterConfig(
        HOST,
        "DIAG-IDENTITY",
        "PRIVATE-ACCESS-CODE",
        model="Kobra S1",
        plugin_id="anycubic",
        printer_id=42,
        options={"tls_fingerprint": PIN},
    )


def ready_connection(config):
    plugin = AnycubicPrinterPlugin()
    connection = plugin.create_connection(config)
    native = connection.native
    native._client = MagicMock()
    native._device_id = "broker-device-id"
    native._subscribed = True
    native.state.connected = True
    native._last_state_time = native._last_message_monotonic = time.monotonic()
    native._report_count = 2
    native._upload_url = f"http://{HOST}:18910/gcode_upload?s=PRIVATE-UPLOAD-SESSION"
    native.connect = MagicMock()
    native.disconnect = MagicMock()
    native.request_status_update = MagicMock()
    native.start_camera = MagicMock()
    return plugin, connection


def statuses(result):
    return {check.id: check.status for check in result.checks}


@pytest.mark.asyncio
async def test_reuses_live_connection_without_commands_pairing_or_disconnect(probes, config, monkeypatch):
    plugin, connection = ready_connection(config)
    create = MagicMock(side_effect=AssertionError("Must not create another client"))
    monkeypatch.setattr(diagnostic, "_new_client", create)
    progress = {}
    # Exercise the real plugin hook with the host-captured connection.
    result = await plugin.connection_diagnostics(config, connection=connection, progress=progress)
    assert result.overall == "ok"
    assert (
        statuses(result)["anycubic_mqtt_auth"]
        == statuses(result)["printer_publishing"]
        == statuses(result)["anycubic_files"]
        == "pass"
    )
    assert statuses(result)["anycubic_ace"] == "skip"
    assert progress["stage"] == "done" and progress["checks"]
    native = connection.native
    native.connect.assert_not_called()
    native.disconnect.assert_not_called()
    native.request_status_update.assert_not_called()
    native.start_camera.assert_not_called()
    native._client.publish.assert_not_called()
    payload = result.model_dump_json()
    for secret in (PIN, "PRIVATE-ACCESS-CODE", "DO-NOT-EXPOSE", "PRIVATE-UPLOAD-SESSION"):
        assert secret not in payload
    camera = next(check for check in result.checks if check.id == "anycubic_camera_tcp")
    assert "no stream or video frame" in camera.message


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"tls_fingerprint": "not-an-approved-pin"}, {"tls_fingerprint": "b" * 64}])
async def test_missing_invalid_or_mismatched_pin_never_attempts_auth(probes, config, monkeypatch, options):
    create = MagicMock(side_effect=AssertionError("No auth without approved matching pin"))
    monkeypatch.setattr(diagnostic, "_new_client", create)
    result = await diagnostic.diagnose(replace(config, options=options))
    assert statuses(result)["anycubic_tls_pin"] == ("warn" if not options else "fail")
    assert statuses(result)["anycubic_mqtt_auth"] == "skip"
    create.assert_not_called()
    assert "automatically" in next(check for check in result.checks if check.id == "anycubic_tls_pin").hint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value,check_id",
    [
        ("cn", "ANOTHER-PRINTER", "anycubic_identity"),
        ("modelId", "20024", "anycubic_model"),
        ("ctrlType", "cloud", "anycubic_lan_mode"),
        ("ctrlInfoUrl", "http://192.168.50.51:18910/ctrl", "anycubic_lan_mode"),
    ],
)
async def test_wrong_identity_model_or_handshake_is_rejected_before_auth(
    probes, config, monkeypatch, field, value, check_id
):
    probes.info[field] = value
    create = MagicMock(side_effect=AssertionError("Wrong target must not pair"))
    monkeypatch.setattr(diagnostic, "_new_client", create)
    result = await diagnostic.diagnose(config)
    assert result.overall == "problems" and statuses(result)[check_id] == "fail"
    assert statuses(result)["anycubic_mqtt_auth"] == "skip"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_saved_verified_broker_id_alias_is_accepted(probes, config):
    config = replace(config, serial_number="broker-device-id")
    plugin, connection = ready_connection(config)
    result = await plugin.connection_diagnostics(config, connection=connection)
    assert statuses(result)["anycubic_identity"] == "pass"
    connection.native.connect.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_live_mqtt_remains_visible_when_http_service_fails(probes, config):
    probes.read.return_value = None
    plugin, connection = ready_connection(config)
    result = await plugin.connection_diagnostics(config, connection=connection)
    assert result.overall == "problems"
    assert statuses(result)["anycubic_http_info"] == "fail"
    assert statuses(result)["anycubic_mqtt_auth"] == statuses(result)["printer_publishing"] == "pass"
    connection.native.connect.assert_not_called()
    connection.native.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_stale_telemetry_is_not_reported_as_fresh_or_file_ready(probes, config):
    plugin, connection = ready_connection(config)
    connection.native._last_state_time = time.monotonic() - connection.native.STALE_TIMEOUT - 1
    result = await plugin.connection_diagnostics(config, connection=connection)
    assert statuses(result)["anycubic_mqtt_auth"] == "pass"
    assert statuses(result)["printer_publishing"] == statuses(result)["anycubic_files"] == "warn"
    assert result.overall == "warnings"


@pytest.mark.asyncio
async def test_absent_optional_ace_is_skipped_but_required_ace_fails(probes, config):
    plugin, connection = ready_connection(config)
    optional = await plugin.connection_diagnostics(config, connection=connection)
    assert statuses(optional)["anycubic_ace"] == "skip"
    required_config = replace(config, options={**config.options, "require_ace": True})
    required_plugin, required_connection = ready_connection(required_config)
    required = await required_plugin.connection_diagnostics(required_config, connection=required_connection)
    assert statuses(required)["anycubic_ace"] == "fail"
    assert required.overall == "problems"
    connection.native._telemetry.boxes[0] = {"id": 0, "tray": []}
    stale = await plugin.connection_diagnostics(config, connection=connection)
    assert statuses(stale)["anycubic_ace"] == "warn"


@pytest.mark.asyncio
async def test_fresh_ace_information_passes_without_requesting_a_refresh(probes, config, monkeypatch):
    plugin, connection = ready_connection(config)
    connection.native._telemetry.boxes[0] = {"id": 0, "tray": []}
    monkeypatch.setattr(connection.native, "ace_material_snapshot", lambda: [{"id": 0, "tray": []}])
    result = await plugin.connection_diagnostics(config, connection=connection)
    assert statuses(result)["anycubic_ace"] == "pass"
    connection.native.request_status_update.assert_not_called()


@pytest.mark.asyncio
async def test_isolated_unsaved_connection_is_always_cleaned_up(probes, config, monkeypatch):
    config = replace(config, printer_id=None)
    _, temporary = ready_connection(config)
    factory = MagicMock(return_value=temporary.native)
    monkeypatch.setattr(diagnostic, "_new_client", factory)
    result = await diagnostic.diagnose(config, wait_for_publish_seconds=0.01)
    assert result.overall == "ok"
    temporary.native.connect.assert_called_once_with()
    temporary.native.disconnect.assert_called_once_with(timeout=diagnostic.CLEANUP_SECONDS)
    temporary.native._client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_failed_temporary_auth_cleans_up_and_does_not_expose_errors(probes, config, monkeypatch):
    _, temporary = ready_connection(config)
    temporary.native.connect.side_effect = RuntimeError("password=PRIVATE-FAILURE-DETAILS")
    monkeypatch.setattr(diagnostic, "_new_client", lambda *_: temporary.native)
    result = await diagnostic.diagnose(replace(config, printer_id=None))
    assert statuses(result)["anycubic_mqtt_auth"] == "fail"
    assert "PRIVATE-FAILURE-DETAILS" not in result.model_dump_json()
    temporary.native.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_cancellation_waits_for_temporary_cleanup_to_finish(probes, config, monkeypatch):
    _, temporary = ready_connection(config)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    waiting = asyncio.Event()

    async def observe(*_):
        waiting.set()
        await asyncio.Event().wait()

    def disconnect(*, timeout):
        entered.set()
        assert release.wait(5)
        finished.set()

    temporary.native.disconnect = disconnect
    monkeypatch.setattr(diagnostic, "_new_client", lambda *_: temporary.native)
    monkeypatch.setattr(diagnostic, "_observe", observe)
    task = asyncio.create_task(diagnostic.diagnose(replace(config, printer_id=None)))
    try:
        await waiting.wait()
        task.cancel()
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_unsupported_address_never_uses_network(probes, config):
    result = await diagnostic.diagnose(replace(config, ip_address="8.8.8.8"))
    assert statuses(result) == {"anycubic_address": "fail"}
    probes.read.assert_not_awaited()
    probes.tcp.assert_not_awaited()


@pytest.mark.asyncio
async def test_tcp_and_certificate_probes_are_bounded_and_close_connections(monkeypatch):
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.get_extra_info.return_value.getpeercert.return_value = b"synthetic-certificate"
    connect = AsyncMock(return_value=(None, writer))
    monkeypatch.setattr(diagnostic.asyncio, "open_connection", connect)
    assert await diagnostic._tcp(HOST, 9883)
    assert await diagnostic._certificate(HOST) == hashlib.sha256(b"synthetic-certificate").hexdigest()
    assert writer.close.call_count == writer.wait_closed.await_count == 2
    kwargs = connect.call_args.kwargs
    assert kwargs["ssl_handshake_timeout"] == diagnostic.PORT_TIMEOUT
    assert kwargs["ssl"].check_hostname is False
    connect.side_effect = OSError("secret network detail")
    assert not await diagnostic._tcp(HOST, 9883)
    assert await diagnostic._certificate(HOST) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b'{"modelId":"20025"}', {"modelId": "20025"}),
        (302, b"PRIVATE-REDIRECT-LOCATION", None),
        (200, b"not-json-PRIVATE-ERROR", None),
        (200, b"x" * (diagnostic.MAX_BYTES + 1), None),
    ],
)
async def test_http_probe_is_direct_bounded_and_does_not_follow_redirects(monkeypatch, status, body, expected):
    observed = {}

    class Response:
        def __init__(self):
            self.status = status
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def iter_chunked(self, size):
            for offset in range(0, len(body), size):
                yield body[offset : offset + size]

    class Session:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def get(self, url, **kwargs):
            observed.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(diagnostic.aiohttp, "ClientSession", Session)
    assert await diagnostic._info(HOST) == expected
    assert observed["trust_env"] is False and observed["allow_redirects"] is False
    assert observed["timeout"].total == diagnostic.INFO_TIMEOUT
    assert observed["url"] == f"http://{HOST}:18910/info"


def test_temporary_native_factory_has_no_host_hooks_and_only_requests_readonly_status(config):
    native = diagnostic._new_client(config, PIN)
    assert native.on_state_change is None and not hasattr(native, "journal")
    native._client = MagicMock()
    native._client.publish.return_value.rc = 0
    native._device_id = "fixture-device"
    native._subscribed_callback(native._generation, native._client, [0])
    packets = [json.loads(call.args[1]) for call in native._client.publish.call_args_list]
    assert {(packet["type"], packet["action"]) for packet in packets} == {
        ("info", "query"),
        ("tempature", "query"),
        ("fan", "query"),
        ("light", "query"),
        ("multiColorBox", "getInfo"),
        ("extrudeControl", "getInfo"),
    }
    assert all(packet["data"] is None for packet in packets)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["saved_model", "saved_pin", "native_model", "native_pin"])
async def test_live_session_is_not_reused_for_a_changed_model_or_pin(probes, config, monkeypatch, changed):
    plugin, connection = ready_connection(config)
    if changed == "saved_model":
        config = replace(config, model="Kobra 3")
        probes.info.update(modelId="20024", modelName="Kobra 3")
    elif changed == "saved_pin":
        config = replace(config, options={"tls_fingerprint": "b" * 64})
        probes.certificate.return_value = "b" * 64
    elif changed == "native_model":
        connection.native.model = "Kobra 3"
    else:
        connection.native._fingerprint = "b" * 64
    factory = MagicMock(side_effect=AssertionError("Mismatched supplied session must not be replaced silently"))
    monkeypatch.setattr(diagnostic, "_new_client", factory)
    result = await plugin.connection_diagnostics(config, connection=connection)
    assert statuses(result)["anycubic_connection_binding"] == "fail"
    assert statuses(result)["anycubic_mqtt_auth"] == statuses(result)["printer_publishing"] == "skip"
    connection.native.connect.assert_not_called()
    connection.native.disconnect.assert_not_called()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_host_none_never_falls_back_to_an_old_cached_session(probes, config, monkeypatch):
    plugin, old = ready_connection(config)
    desired = replace(config, options={"tls_fingerprint": "b" * 64})
    probes.certificate.return_value = "b" * 64
    _, temporary = ready_connection(desired)
    factory = MagicMock(return_value=temporary.native)
    monkeypatch.setattr(diagnostic, "_new_client", factory)
    result = await plugin.connection_diagnostics(desired, connection=None, wait_for_publish_seconds=0.01)
    assert statuses(result)["anycubic_mqtt_auth"] == "pass"
    factory.assert_called_once_with(desired, "b" * 64)
    assert plugin.connections[config.serial_number] is old
    old.native.connect.assert_not_called()
    old.native.disconnect.assert_not_called()
    old.native._client.publish.assert_not_called()
    temporary.native.connect.assert_called_once()
    temporary.native.disconnect.assert_called_once()

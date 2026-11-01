"""Real Paho queues/locks with in-memory sockets; never contact a printer."""

import errno
import json
import threading
import time
from unittest.mock import patch

import paho.mqtt.client as mqtt
import pytest

from backend.app.printers.contracts import PrinterConfig
from plugins.anycubic.payload import client as client_module
from plugins.anycubic.payload.client import _SessionMQTTClient
from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin
from plugins.anycubic.payload.protocol import AnycubicError


class MemorySocket:
    def __init__(self, *, broken=False):
        self.broken = broken
        self.writes = []
        self.closed = threading.Event()
        self.close_count = 0

    def send(self, data):
        self.writes.append(bytes(data))
        if self.broken or self.closed.is_set():
            raise OSError(errno.EPIPE, "fixture socket failed")
        return len(data)

    def recv(self, _size):
        raise BlockingIOError

    def close(self):
        self.close_count += 1
        self.closed.set()


@pytest.fixture
def session():
    plugin = AnycubicPrinterPlugin()
    config = PrinterConfig(
        "192.168.50.50",
        "MQTT-LOCK-FIXTURE",
        "",
        model="Kobra S1",
        plugin_id="anycubic",
        options={"tls_fingerprint": "a" * 64},
    )
    connection = plugin.create_connection(config)
    native = connection.native
    paho = _SessionMQTTClient()
    native._client = paho
    native._subscribed = True
    native._device_id = "fixture-device"
    native.state.connected, native.state.state = True, "IDLE"
    native._last_message_monotonic = native._last_state_time = native._info_observed = time.monotonic()
    native.on_state_change = None
    return plugin, config, native, paho


def drive_manual_loop(monkeypatch, paho, stream):
    monkeypatch.setattr(mqtt, "_socketpair_compat", lambda: (MemorySocket(), MemorySocket()))
    monkeypatch.setattr(mqtt.select, "select", lambda _read, write, _error, _timeout: ([], write, []))
    return paho.loop(timeout=0)


def test_camera_publish_cannot_invert_paho_disconnect_callback_and_device_state_locks(session, monkeypatch):
    """The NAS interleaving deadlocks if publish() performs socket I/O inline."""
    plugin, config, native, paho = session
    stream = paho._sock = MemorySocket(broken=True)
    handoff, callback_entered = threading.Event(), threading.Event()
    failures, camera_errors, descriptors, inline_writers = [], [], [], []
    real_write, real_publish = paho.loop_write, paho.publish

    def gated_write(*args, **kwargs):
        inline_writers.append(threading.current_thread().name)
        handoff.set()
        assert callback_entered.wait(1)
        return real_write(*args, **kwargs)

    def publish(*args, **kwargs):
        try:
            return real_publish(*args, **kwargs)
        finally:
            handoff.set()

    def disconnected(client, *_args):
        callback_entered.set()
        native._disconnected(0, client)

    def network_callback():
        try:
            assert handoff.wait(1)
            # Uses Paho's real non-reentrant _in_callback_mutex, as on the NAS.
            paho._do_on_disconnect(False, mqtt.MQTT_ERR_CONN_LOST)
        except BaseException as error:
            failures.append(error)

    def camera():
        try:
            native.start_camera(timeout=0.1)
        except AnycubicError as error:
            camera_errors.append(error)
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(paho, "loop_write", gated_write)
    monkeypatch.setattr(paho, "publish", publish)
    paho.on_disconnect = disconnected
    network = threading.Thread(target=network_callback, name="fixture-mqtt-callback", daemon=True)
    publisher = threading.Thread(target=camera, name="fixture-camera", daemon=True)
    network.start()
    publisher.start()
    assert callback_entered.wait(1)
    describe = threading.Thread(target=lambda: descriptors.append(plugin.describe_device(config)), daemon=True)
    describe.start()
    publisher.join(1)
    network.join(1)
    describe.join(1)
    assert not publisher.is_alive(), "Camera publish is deadlocked against Paho's disconnect callback"
    assert not network.is_alive(), "Disconnect callback is waiting for the publisher's native lock"
    assert not describe.is_alive(), "The device-state HTTP path is blocked behind the MQTT locks"
    assert not failures
    assert camera_errors and descriptors
    assert inline_writers == [] and stream.writes == []


@pytest.mark.parametrize("broken", [False, True])
def test_only_the_session_loop_writes_queued_packets_and_handles_disconnect(session, monkeypatch, broken):
    _plugin, _config, native, paho = session
    stream = paho._sock = MemorySocket(broken=broken)
    disconnect_threads = []
    paho.on_disconnect = lambda client, *_args: (
        disconnect_threads.append(threading.current_thread().name),
        native._disconnected(0, client),
    )
    assert native._publish("info", "query", None)
    assert paho.want_write() and stream.writes == [] and disconnect_threads == []
    result = drive_manual_loop(monkeypatch, paho, stream)
    assert len(stream.writes) == 1
    if broken:
        assert result == mqtt.MQTT_ERR_CONN_LOST
        assert disconnect_threads == [threading.current_thread().name]
        assert native.state.connected is False
    else:
        assert result == mqtt.MQTT_ERR_SUCCESS
        assert not paho.want_write() and disconnect_threads == []


def test_subscription_is_also_queued_without_inline_socket_io(session, monkeypatch):
    _plugin, _config, native, paho = session
    stream = paho._sock = MemorySocket()
    native._subscribed = False
    native._connected(0, paho, 0)
    assert paho.want_write() and stream.writes == []
    assert drive_manual_loop(monkeypatch, paho, stream) == mqtt.MQTT_ERR_SUCCESS
    assert len(stream.writes) == 1


def test_retirement_closes_and_discards_unsent_print_without_replaying_on_replacement(session, monkeypatch):
    _plugin, _config, native, paho = session
    stream = paho._sock = MemorySocket()
    assert native._publish("print", "start", {"filename": "never-send-fixture.gcode"})
    assert paho.want_write() and stream.writes == []
    generation = native._generation
    native.disconnect()
    assert stream.closed.wait(1), "Retirement must close the manual-loop transport"
    assert paho.socket() is None and not paho.want_write() and stream.writes == []
    replacement = _SessionMQTTClient()
    replacement_stream = replacement._sock = MemorySocket()
    native._client, native._subscribed = replacement, True
    native._stop = threading.Event()
    native.state.connected = True
    native._disconnected(generation, paho)
    assert native.state.connected is True
    assert drive_manual_loop(monkeypatch, replacement, replacement_stream) == mqtt.MQTT_ERR_SUCCESS
    assert replacement_stream.writes == []
    replacement.disconnect()


def test_concurrent_repeated_retirements_do_not_flush_or_close_twice():
    paho = _SessionMQTTClient()
    stream = paho._sock = MemorySocket()
    paho.publish("fixture/print", "never send", qos=0)
    threads = [threading.Thread(target=paho.disconnect, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
    assert all(not thread.is_alive() for thread in threads)
    assert stream.close_count == 1 and stream.writes == []
    assert paho.socket() is None and not paho.want_write()


def wire_commands(stream):
    commands = []
    for packet in stream.writes:
        offset = 1
        while packet[offset] & 128:
            offset += 1
        offset += 1
        length = int.from_bytes(packet[offset : offset + 2])
        offset += 2
        commands.append((packet[offset : offset + length].decode(), json.loads(packet[offset + length :])))
    return commands


@pytest.mark.parametrize("timer_finished", [False, True])
def test_retiring_an_active_pulse_sends_only_fresh_stop_on_its_old_transport(session, monkeypatch, timer_finished):
    _plugin, _config, native, paho = session
    stream = paho._sock = MemorySocket()
    native._extrude_observed = time.monotonic()
    native._extrude_operate = 0
    native.state.temperatures["nozzle"] = 200
    with patch.object(client_module.threading, "Timer") as timer:
        assert native.extruder_pulse(1)
        assert drive_manual_loop(monkeypatch, paho, stream) == mqtt.MQTT_ERR_SUCCESS
        assert wire_commands(stream)[0][1]["data"] == {"operate_type": 1, "move_type": 1}
        stream.writes.clear()
        finish = timer.call_args.args[1]
        if timer_finished:
            finish()
            assert native._extrusion_timer is None and native._extrusion_pending
        assert native._publish("print", "start", {"filename": "must-not-replay.gcode"})
        stale_messages = {
            json.loads(packet["packet"][packet["packet"].find(b"{") :])["msgid"] for packet in paho._out_packet
        }
        native.disconnect()
        assert stream.closed.wait(1)
        commands = wire_commands(stream)
        assert len(commands) == 1
        topic, envelope = commands[0]
        assert topic.endswith("/fixture-device/extrudeControl")
        assert envelope["type"] == "extrudeControl" and envelope["action"] == "set"
        assert envelope["data"] == {"operate_type": 0}
        assert envelope["msgid"] not in stale_messages
        assert native._extrusion_pending and native._extrude_operate is None
        assert not native.state.connected and native.state.state == "unknown"
        assert paho.socket() is None and not paho.want_write()
        paho.disconnect()
        assert len(stream.writes) == 1
        replacement = _SessionMQTTClient()
        replacement_stream = replacement._sock = MemorySocket()
        native._client, native._device_id, native._subscribed = replacement, "replacement-device", True
        native._stop = threading.Event()
        native.state.connected = True
        finish()  # The cancelled old timer must not issue a replacement-session stop.
        assert not replacement.want_write() and replacement_stream.writes == []
        replacement.disconnect()


def test_broken_retirement_stop_closes_without_confirming_motion_stopped(session, caplog):
    _plugin, _config, native, paho = session
    stream = paho._sock = MemorySocket(broken=True)
    native._extrusion_pending = True
    native._extrude_operate = None
    assert native._publish("print", "start", {"filename": "discard.gcode"})
    native.disconnect()
    assert stream.closed.wait(1)
    assert len(stream.writes) == 1
    assert wire_commands(stream)[0][1]["data"] == {"operate_type": 0}
    assert native._extrusion_pending and native._extrude_operate is None
    # Socket.close signals before the retirement worker's final queue cleanup.
    # Wait for that same retirement to finish before inspecting its result.
    with paho._retirement_lock:
        assert paho.socket() is None and not paho.want_write()
    assert "could not be sent on the retiring session" in caplog.text


def test_partial_old_frame_is_not_completed_or_followed_by_a_stop(session, caplog):
    _plugin, _config, native, paho = session
    stream = paho._sock = MemorySocket()
    native._extrusion_pending = True
    assert native._publish("print", "start", {"filename": "discard-partial.gcode"})
    packet = paho._out_packet[0]
    packet["pos"] = 1
    packet["to_process"] -= 1
    stream.writes.append(packet["packet"][:1])
    before = list(stream.writes)
    native.disconnect()
    assert stream.closed.wait(1)
    assert stream.writes == before
    assert native._extrusion_pending and native._extrude_operate is None
    assert paho.socket() is None and not paho.want_write()
    assert "partially written MQTT packet" in caplog.text


def test_stop_preparation_never_takes_transport_mutex_while_holding_native_lock(session, monkeypatch):
    _plugin, _config, native, paho = session
    stream = paho._sock = MemorySocket()
    native._extrusion_pending = True
    writer_entered, permit_callback, callback_entered = (threading.Event() for _ in range(3))
    failures = []
    prepare = native._prepare_extrusion_retirement

    def callback(client, *_args):
        callback_entered.set()
        native._disconnected(0, client)

    def gated_write(client, _max_packets=1):
        if threading.current_thread().name == "fixture-owner":
            writer_entered.set()
            assert permit_callback.wait(1)
            client._do_on_disconnect(False, mqtt.MQTT_ERR_CONN_LOST)
        return mqtt.MQTT_ERR_CONN_LOST

    def gated_prepare(client):
        permit_callback.set()
        assert callback_entered.wait(1)
        prepare(client)

    def run(operation):
        try:
            operation()
        except BaseException as error:
            failures.append(error)

    paho.on_disconnect = callback
    monkeypatch.setattr(mqtt.Client, "loop_write", gated_write)
    monkeypatch.setattr(native, "_prepare_extrusion_retirement", gated_prepare)
    owner = threading.Thread(target=lambda: run(paho.loop_write), name="fixture-owner", daemon=True)
    detacher = threading.Thread(target=lambda: run(native.disconnect), daemon=True)
    owner.start()
    assert writer_entered.wait(1)
    detacher.start()
    owner.join(1)
    detacher.join(1)
    assert not owner.is_alive() and not detacher.is_alive()
    assert not failures and stream.closed.wait(1)
    assert native._client is None and not native.state.connected

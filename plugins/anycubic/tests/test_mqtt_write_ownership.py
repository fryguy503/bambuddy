"""Real Paho queues/locks with in-memory sockets; never contact a printer."""

import errno
import threading
import time

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

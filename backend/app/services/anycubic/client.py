"""Experimental Anycubic Kobra monitoring and basic controls over stock LAN mode."""

import copy
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from backend.app.utils.anycubic_models import get_model
from backend.app.utils.paho_teardown import retire_paho_client

from .protocol import (
    PREFIX,
    AnycubicError,
    PinnedTLSContext,
    fingerprint,
    get_info,
    identity,
    inspect_fingerprint,
    json_object,
    model_from_info,
    pair,
    private_host,
)
from .telemetry import Telemetry

logger = logging.getLogger(__name__)
_QUERIES = (
    ("info", "query"),
    ("tempature", "query"),
    ("fan", "query"),
    ("light", "query"),
    ("multiColorBox", "getInfo"),
)


class AnycubicMQTTClient:
    """A display-state adapter; no Bambu commands or archival/spool callbacks.

    A successful control return means queued locally, not accepted by hardware.
    Pairing credentials live only in the current worker and MQTT client.
    """

    printer_type = "anycubic"
    STALE_TIMEOUT = 60

    def __init__(
        self,
        ip_address,
        serial_number,
        access_code="",
        model=None,
        on_state_change=None,
        *,
        tls_fingerprint,
        **callbacks,
    ):
        self.ip_address = private_host(ip_address)
        self.serial_number = serial_number
        selected_model = get_model(model or "Kobra S1")
        if selected_model is None:
            raise AnycubicError("Unsupported Anycubic LAN printer model")
        self.model = selected_model.name
        self._model_id = selected_model.model_id
        self._fingerprint = fingerprint(tls_fingerprint)
        self.on_state_change = on_state_change
        self._telemetry = Telemetry(self.model)
        self.state = self._telemetry.state
        self._client = None
        self._worker = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._generation = 0
        self._subscribed = False
        self._connection_lost = False
        self._device_id = None
        self._last_message_time = 0.0
        self._last_message_monotonic = 0.0
        self._last_state_time = 0.0
        self._report_count = 0
        self.last_connect_error = None
        self._drying_targets = {}
        self._logging_enabled = False
        self._seen_reports = OrderedDict()

    @staticmethod
    def discover(ip_address):
        """Inspect identity and certificate only; user must approve the fingerprint."""
        host = private_host(ip_address)
        info = get_info(host)
        model = model_from_info(info)
        return {
            "serial_number": identity(info),
            "model": model.name,
            "model_id": model.model_id,
            "device_name": f"Anycubic {model.name}",
            "tls_fingerprint": inspect_fingerprint(host),
        }

    @property
    def report_messages_since_connect(self):
        return self._report_count

    @property
    def is_stale(self):
        return bool(
            self._last_message_monotonic and time.monotonic() - self._last_message_monotonic > self.STALE_TIMEOUT
        )

    def _notify(self):
        callback = self.on_state_change
        if callback:
            try:
                callback(copy.deepcopy(self.state))
            except Exception:
                logger.warning("Anycubic status callback failed")

    def connect(self, loop=None):
        self.disconnect()
        with self._lock:
            self._generation += 1
            generation = self._generation
            stop = self._stop = threading.Event()
            self.last_connect_error = None
            self._report_count = 0
            self._last_message_time = self._last_state_time = 0
            self._last_message_monotonic = 0
            self._seen_reports.clear()
            self._telemetry = Telemetry(self.model)
            self.state = self._telemetry.state
            self._worker = threading.Thread(target=self._run, args=(generation, stop), name="anycubic-lan", daemon=True)
            self._worker.start()

    def _current(self, generation, client):
        return generation == self._generation and client is self._client and not self._stop.is_set()

    def _run(self, generation, stop):
        delay = 2
        while not stop.is_set():
            self._run_session(generation, stop)
            if stop.wait(delay):
                return
            # Every reconnect repeats the handshake and checks the same approved
            # pin. No retained commands or failed control intents are replayed.
            delay = min(delay * 2, 60)

    def _run_session(self, generation, stop):
        client = None
        try:
            credentials = pair(self.ip_address, self.serial_number, self._fingerprint)
            if credentials.model_id != self._model_id:
                raise AnycubicError("Printer model does not match the saved printer")
            if stop.is_set():
                return

            class DirectClient(mqtt.Client):
                def _get_proxy(self):
                    return None  # LAN printer credentials must not traverse an environment proxy.

            client = DirectClient(
                CallbackAPIVersion.VERSION2,
                client_id="bambuddy-" + uuid.uuid4().hex,
                protocol=mqtt.MQTTv311,
                clean_session=True,
                reconnect_on_failure=False,
            )
            client.connect_timeout = 5
            client.max_queued_messages_set(32)
            client.username_pw_set(credentials.username, credentials.password)
            client.tls_set_context(PinnedTLSContext(self._fingerprint, self.ip_address, credentials))
            client.on_connect = lambda c, _u, _f, r, _p=None: self._connected(generation, c, r)
            client.on_subscribe = lambda c, _u, _m, r, _p=None: self._subscribed_callback(generation, c, r)
            client.on_message = lambda c, _u, m: self._received(generation, c, m)
            client.on_disconnect = lambda c, *_args: self._disconnected(generation, c)
            with self._lock:
                if stop.is_set() or generation != self._generation:
                    return
                self._client = client
                self._device_id = credentials.device_id
                self._connection_lost = False
                self._last_message_time = self._last_state_time = 0
                self._last_message_monotonic = 0
                self._seen_reports.clear()
                self._report_count = 0
                self._telemetry = Telemetry(self.model)
                self.state = self._telemetry.state
            client.connect(credentials.host, 9883, keepalive=15)
            del credentials
            deadline, refresh_at = time.monotonic() + 20, time.monotonic() + 10
            while not stop.is_set():
                if self._connection_lost:
                    raise AnycubicError("Printer MQTT connection ended")
                if client.loop(timeout=1) != mqtt.MQTT_ERR_SUCCESS:
                    raise AnycubicError("Printer MQTT connection ended")
                now = time.monotonic()
                if not self.state.connected and now > deadline:
                    raise AnycubicError("Printer did not report usable telemetry")
                if now >= refresh_at:
                    self.request_status_update()
                    self.check_staleness()
                    refresh_at = now + 10
        except AnycubicError:
            with self._lock:
                if generation == self._generation:
                    self.last_connect_error = "anycubic_connection_failed"
        except Exception:
            # Third-party exception messages can include URLs, tokens and passwords.
            with self._lock:
                if generation == self._generation:
                    self.last_connect_error = "anycubic_connection_failed"
        finally:
            with self._lock:
                current = generation == self._generation
                if current:
                    self._client = None
                    self._subscribed = False
                    self.state.connected = False
                    self.state.state = "unknown"
                    self.state.raw_data["ams"] = []
            if client is not None:
                retire_paho_client(client, "anycubic")
            if current:
                self._notify()

    def _connected(self, generation, client, reason):
        with self._lock:
            if not self._current(generation, client):
                return
            if reason != 0:
                self.last_connect_error = (
                    "auth_rejected" if getattr(reason, "value", reason) in (4, 5, 134, 135) else "refused"
                )
                self._connection_lost = True
                return
            client.subscribe(f"{PREFIX}/printer/public/{self._model_id}/{self._device_id}/#", qos=0)

    def _subscribed_callback(self, generation, client, reasons):
        with self._lock:
            if not self._current(generation, client):
                return
            if any(getattr(reason, "value", reason) not in (0, 1, 2) for reason in reasons):
                self._connection_lost = True
                self.last_connect_error = "refused"
                return
            self._subscribed = True
        self.request_status_update()

    def _disconnected(self, generation, client):
        with self._lock:
            if not self._current(generation, client):
                return
            self._connection_lost = True
            self._subscribed = False
            self.state.connected = False
        self._notify()

    def _received(self, generation, client, message):
        with self._lock:
            if not self._current(generation, client) or not self._subscribed:
                return
            if getattr(message, "retain", False) is True:
                return  # A retained snapshot is not fresh observation of a new session.
            prefix = f"{PREFIX}/printer/public/{self._model_id}/{self._device_id}/"
            if not isinstance(message.topic, str) or not message.topic.startswith(prefix):
                return
            tail = message.topic[len(prefix) :].split("/")
            if len(tail) != 2 or tail[1] != "report":
                return
            try:
                envelope = json_object(bytes(message.payload))
                report_id = envelope.get("msgid", envelope.get("msgId"))
                if report_id is not None:
                    if not isinstance(report_id, str) or not 1 <= len(report_id) <= 128:
                        return
                    key = (tail[0], report_id)
                    if key in self._seen_reports:
                        return
                if envelope.get("type") != tail[0] or not self._telemetry.apply(tail[0], envelope):
                    return
            except (AnycubicError, TypeError, ValueError, RecursionError):
                return
            self._last_message_time = time.time()
            self._last_message_monotonic = time.monotonic()
            if self._telemetry.lifecycle_observed:
                self._last_state_time = self._last_message_monotonic
            if report_id is not None:
                self._seen_reports[(tail[0], report_id)] = None
                if len(self._seen_reports) > 512:
                    self._seen_reports.popitem(last=False)
            self._report_count += 1
            self.state.connected = True
            self.last_connect_error = None
        self._notify()

    def _publish(self, kind, action, data):
        with self._lock:
            if self._client is None or not self._subscribed or self._stop.is_set():
                return False
            envelope = {
                "type": kind,
                "action": action,
                "data": data,
                "timestamp": int(time.time() * 1000),
                "msgid": str(uuid.uuid4()),
            }
            try:
                result = self._client.publish(
                    f"{PREFIX}/web/printer/{self._model_id}/{self._device_id}/{kind}",
                    json.dumps(envelope),
                    qos=0,
                    retain=False,
                )
                return result.rc == mqtt.MQTT_ERR_SUCCESS
            except Exception:
                return False

    def request_status_update(self):
        return all(self._publish(kind, action, None) for kind, action in _QUERIES)

    def _print_control(self, action, allowed):
        with self._lock:
            if (
                not self.state.connected
                or self.is_stale
                or self.state.state not in allowed
                or self._telemetry.phase
                not in {
                    "pause": {"printing", "resumed", "preheating", "auto_leveling", "vibrating", "flow_calibrating"},
                    "resume": {"paused"},
                    "stop": {
                        "printing",
                        "resumed",
                        "resuming",
                        "paused",
                        "pausing",
                        "preheating",
                        "auto_leveling",
                        "vibrating",
                        "flow_calibrating",
                        "downloading",
                        "checking",
                    },
                }[action]
                or not 0 <= time.monotonic() - self._last_state_time <= 30
            ):
                return False
            return self._publish("print", action, {"taskid": self._telemetry.task_id})

    def pause_print(self):
        return self._print_control("pause", {"RUNNING", "PREPARE"})

    def resume_print(self):
        return self._print_control("resume", {"PAUSE"})

    def stop_print(self):
        return self._print_control("stop", {"RUNNING", "PREPARE", "PAUSE"})

    def disconnect(self, timeout: float = 0):
        with self._lock:
            self._generation += 1
            self._stop.set()
            client, worker = self._client, self._worker
            self._client = None
            self._subscribed = False
            self._device_id = None
            self.state.connected = False
            self.state.state = "unknown"
            self.state.raw_data["ams"] = []
            self._telemetry.boxes = {}
        if client is not None:
            retire_paho_client(client, "anycubic")
        if worker is not None and timeout > 0 and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def check_staleness(self):
        with self._lock:
            if not self.is_stale or not self.state.connected:
                return False
            self.state.connected = False
            self.state.state = "unknown"
            self.state.raw_data["ams"] = []
            self._telemetry.boxes = {}
        self._notify()
        return True

    def mark_power_off(self):
        was_connected = self.state.connected
        self.disconnect()
        return was_connected

    def force_reconnect_stale_session(self, reason):
        self.connect()

    def enable_logging(self, enabled=True):
        self._logging_enabled = bool(enabled)

    @property
    def logging_enabled(self):
        return self._logging_enabled

    def get_logs(self):
        return []  # Never retain the secret-bearing source envelopes.

    def clear_logs(self):
        pass

    def register_raw_message_handler(self, handler):
        pass  # No Bambu virtual-printer/raw-message bridge.

    def unregister_raw_message_handler(self, handler):
        pass

    def _unsupported(self, *args, **kwargs):
        return False

    start_print = send_command = send_gcode = publish_raw = _unsupported
    send_drying_command = set_xcam_option = start_calibration = skip_objects = _unsupported
    clear_hms_errors = execute_hms_action = set_timelapse = set_liveview = _unsupported
    set_bed_temperature = set_nozzle_temperature = set_chamber_temperature = _unsupported
    set_print_speed = set_fan_speed = set_part_fan = set_aux_fan = set_chamber_fan = _unsupported
    set_left_aux_fan = set_airduct_mode = set_chamber_light = select_extruder = _unsupported
    home_axes = move_axis = disable_motors = enable_motors = _unsupported
    ams_load_filament = ams_unload_filament = ams_control = ams_set_filament_setting = _unsupported
    reset_ams_slot = extrusion_cali_sel = extrusion_cali_set = set_ams_filament_backup = _unsupported
    set_kprofile = set_kprofiles_batch = delete_kprofile = register_assignment_verification = _unsupported

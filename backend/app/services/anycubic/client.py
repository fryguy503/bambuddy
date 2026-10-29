"""Experimental Anycubic Kobra monitoring and basic controls over stock LAN mode."""

import copy
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import PurePosixPath

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from backend.app.utils.anycubic_models import get_model
from backend.app.utils.paho_teardown import retire_paho_client

from .drying import limits as drying_limits
from .files import FileSession, directory, parse_catalog, upload_url
from .printing import OPTION_FEATURES, PrintArtifact, print_data, rgba, validate_options
from .protocol import (
    PREFIX,
    AnycubicError,
    PinnedTLSContext,
    camera_stream_url,
    fingerprint,
    get_info,
    identity,
    inspect_fingerprint,
    json_object,
    model_from_info,
    pair,
    private_host,
    task_id,
)
from .telemetry import Telemetry

logger = logging.getLogger(__name__)
_QUERIES = (
    ("info", "query"),
    ("tempature", "query"),
    ("fan", "query"),
    ("light", "query"),
    ("multiColorBox", "getInfo"),
    ("extrudeControl", "getInfo"),
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
        self._model = selected_model
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
        self._camera_lock = threading.Lock()
        self._camera_event = threading.Event()
        self._camera_requested = False
        self._camera_url = None
        self._camera_result = None
        self._camera_started_at = 0.0
        self._control_limits = {}
        self._reported_fans = {}
        self._reported_fan_fields = set()
        self._ace_drying_seen = {}
        self._ace_seen = {}
        self._ace_material_seen = {}
        self._reported_lights = {}
        self._info_observed = self._extrude_observed = 0.0
        self._extrude_operate = None
        self._extrusion_pending = False
        self._extrusion_timer = None
        self._manual_feed = None
        self._upload_url = None
        self._info_event = threading.Event()
        self._file_lock = threading.Lock()
        self._file_pending = None
        self._print_features = {}
        self._print_features_seen = 0.0
        self._print_pending = None
        self._print_attempts = OrderedDict()

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
    def tls_fingerprint(self):
        return self._fingerprint

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
            self._reset_files()
            self._control_limits.clear()
            self._reported_fans.clear()
            self._reported_fan_fields.clear()
            self._ace_drying_seen.clear()
            self._reset_manual_controls()
            self._reset_camera()
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
                self._reset_files()
                self._device_id = credentials.device_id
                self._connection_lost = False
                self._last_message_time = self._last_state_time = 0
                self._last_message_monotonic = 0
                self._seen_reports.clear()
                self._report_count = 0
                self._telemetry = Telemetry(self.model)
                self.state = self._telemetry.state
                self._control_limits.clear()
                self._reported_fans.clear()
                self._reported_fan_fields.clear()
                self._ace_drying_seen.clear()
                self._reset_manual_controls()
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
                    self._ace_material_seen.clear()
                    self._reset_camera()
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
            self._ace_material_seen.clear()
            self._reset_files()
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
                if envelope.get("type") != tail[0]:
                    return
                control_changed = self._control_report(tail[0], envelope)
                self._file_report(tail[0], envelope)
                self._camera_report(tail[0], envelope)
                self._print_report(tail[0], envelope)
                if not self._telemetry.apply(tail[0], envelope) and not control_changed:
                    return
                if tail[0] == "multiColorBox":
                    self._observe_ace_drying(envelope)
            except (AnycubicError, TypeError, ValueError, RecursionError):
                return
            self._last_message_time = time.time()
            self._last_message_monotonic = time.monotonic()
            if self._telemetry.lifecycle_observed:
                self._last_state_time = self._last_message_monotonic
                self._observe_print_start()
            if report_id is not None:
                self._seen_reports[(tail[0], report_id)] = None
                if len(self._seen_reports) > 512:
                    self._seen_reports.popitem(last=False)
            self._report_count += 1
            self.state.connected = True
            self.state.raw_data["anycubic_controls"] = self.control_capabilities()
            self.last_connect_error = None
        self._notify()

    def _publish(self, kind, action, data, *, msgid=None):
        with self._lock:
            if self._client is None or not self._subscribed or self._stop.is_set():
                return False
            envelope = {
                "type": kind,
                "action": action,
                "data": data,
                "timestamp": int(time.time() * 1000),
                "msgid": msgid or str(uuid.uuid4()),
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

    def _reset_files(self):
        self._reset_print_request()
        self._print_features.clear()
        self._print_features_seen = 0.0
        self._upload_url = None
        self._info_event.set()
        if self._file_pending is not None:
            self._file_pending["error"] = "The Anycubic file session ended"
            self._file_pending["event"].set()
            self._file_pending = None

    def _file_report(self, kind, envelope):
        data = envelope.get("data")
        if kind == "info" and isinstance(data, dict) and envelope.get("code", 200) in (0, 200):
            urls = data.get("urls")
            if isinstance(urls, dict) and "fileUploadurl" in urls:
                try:
                    self._upload_url = upload_url(self.ip_address, urls["fileUploadurl"])
                except AnycubicError:
                    self._upload_url = None
                self._info_event.set()
        pending = self._file_pending
        if kind != "file" or pending is None or envelope.get("action") != "listLocal":
            return
        if envelope.get("code", 200) not in (0, 200):
            pending["error"] = "The printer refused the file catalog request"
            pending["event"].set()
        elif isinstance(data, dict) and "records" in data:
            try:
                pending["result"] = parse_catalog(
                    data, pending["page"], pending["limit"], pending["path"], pending.get("keyword", "")
                )
            except AnycubicError:
                pending["error"] = "The printer returned an unsupported file catalog"
            pending["event"].set()

    def file_session_valid(self, context):
        with self._lock:
            return bool(
                self._current(context.generation, context.connection)
                and self._subscribed
                and self.state.connected
                and not self.is_stale
            )

    def file_session(self, *, require_upload=False, timeout=8.0):
        with self._lock:
            if (
                self._client is None
                or not self.state.connected
                or self.is_stale
                or not self._subscribed
                or self._stop.is_set()
            ):
                raise AnycubicError("Connect the Anycubic printer before browsing or uploading files")
            generation, connection = self._generation, self._client
            if require_upload:
                self._upload_url = None
                self._info_event.clear()
                self._publish("info", "query", None)
        if require_upload:
            self._info_event.wait(timeout)
        with self._lock:
            context = FileSession(self.ip_address, self.model, generation, connection, self._upload_url or "")
            if not self.file_session_valid(context):
                raise AnycubicError("The Anycubic file session ended")
            if require_upload and not context.url:
                raise AnycubicError("The printer did not advertise a LAN upload endpoint")
            return context

    def list_files(self, path="/", page=1, limit=100, keyword="", timeout=8.0):
        """One outstanding read per connection; a catalog grants no print authority."""
        path = directory(path)
        if type(page) is not int or not 1 <= page <= 10000 or type(limit) is not int or not 1 <= limit <= 100:
            raise AnycubicError("Invalid file page")
        if not isinstance(keyword, str) or len(keyword.encode("utf-8")) > 128 or any(ord(c) < 32 for c in keyword):
            raise AnycubicError("Invalid file search")
        if not self._file_lock.acquire(timeout=timeout):
            raise AnycubicError("Another file catalog request is still active")
        try:
            context = self.file_session()
            pending = {
                "event": threading.Event(),
                "page": page,
                "limit": limit,
                "path": path,
                "keyword": keyword,
                "result": None,
                "error": None,
            }
            with self._lock:
                self._file_pending = pending
                queued = self._publish(
                    "file", "listLocal", {"page_num": page, "page_size": limit, "path": path, "keyword": keyword}
                )
            try:
                if not queued or not pending["event"].wait(timeout):
                    raise AnycubicError("The printer file catalog did not respond")
                if not self.file_session_valid(context) or pending["error"] or pending["result"] is None:
                    raise AnycubicError(pending["error"] or "The file session changed")
                return pending["result"]
            finally:
                with self._lock:
                    if self._file_pending is pending:
                        self._file_pending = None
        finally:
            self._file_lock.release()

    def _reset_print_request(self):
        pending = self._print_pending
        if pending is not None and not pending["event"].is_set():
            pending["result"]["status"] = "unknown"
            pending["event"].set()

    def _print_report(self, kind, envelope):
        pending = self._print_pending
        if (
            pending is None
            or kind != "print"
            or envelope.get("action") != "start"
            or envelope.get("msgid", envelope.get("msgId")) != pending["result"]["msgid"]
            or pending["event"].is_set()
        ):
            return
        code = envelope.get("code")
        if type(code) is not int:
            return
        if code not in (0, 200):
            pending["result"]["status"] = "rejected"
            pending["event"].set()
            return
        data = envelope.get("data")
        if not isinstance(data, dict):
            return
        task = task_id(data.get("taskid", data.get("task_id")))
        if task is None or data.get("filename", pending["result"]["filename"]) != pending["result"]["filename"]:
            return
        pending["result"].update(status="accepted", task_id=task)
        pending["event"].set()

    def _observe_print_start(self):
        """Settle local receipt holds when fresh telemetry proves their unique job."""
        if self.state.state not in {"RUNNING", "PREPARE", "PAUSE"}:
            return
        filename = PurePosixPath((self.state.gcode_file or "").replace("\\", "/")).name
        reported = task_id(self.state.subtask_id)
        for attempt in self._print_attempts.values():
            result = attempt["result"]
            assigned = task_id(result["task_id"])
            if (
                result["status"] != "unknown"
                or not filename
                or filename != result["filename"]
                or reported is not None
                and assigned is not None
                and reported != assigned
            ):
                continue
            result["status"] = "accepted"
            if reported is not None:
                result["task_id"] = reported
            attempt["event"].set()

    def print_capabilities(self):
        """Observed features and physical material sources, without session secrets."""
        with self._lock:
            now = time.monotonic()
            options = [
                name
                for name, feature in OPTION_FEATURES.items()
                if self._print_features.get(feature) is True
                and self._print_features_seen
                and now - self._print_features_seen <= self.STALE_TIMEOUT
            ]
            slots = []
            inventory_fresh = bool(
                self._fresh_connection()
                and self._telemetry.boxes
                and all(
                    box_id in self._ace_seen and now - self._ace_seen[box_id] <= self.STALE_TIMEOUT
                    for box_id in self._telemetry.boxes
                )
            )
            # Native 2.0.0.3 sorts signed box IDs before deriving wire indices.
            # A sparse identity (e.g. box 3 alone) is not multiplied by four.
            for position, box in enumerate(sorted(self._telemetry.boxes.values(), key=lambda item: item["id"])):
                fresh = bool(
                    inventory_fresh
                    and box["id"] in self._ace_seen
                    and now - self._ace_seen[box["id"]] <= self.STALE_TIMEOUT
                )
                for tray in box["tray"]:
                    material = tray.get("tray_type") or ""
                    try:
                        color = list(rgba(tray.get("tray_color")))
                    except AnycubicError:
                        color = []
                    slots.append(
                        {
                            "box_id": box["id"],
                            "slot_id": tray["id"],
                            "ams_index": position * 4 + tray["id"],
                            "material_type": material,
                            "color": color,
                            "available": bool(fresh and tray.get("state") == 3 and material and color),
                        }
                    )
            return {
                "ready": bool(self._motion_ready() and self._print_pending is None),
                "options": options,
                "ace_slots": slots,
                "use_ace": any(item["available"] for item in slots),
            }

    def submit_print(self, context, artifact, options, dispatch_id, timeout=20.0, *, cancelled=None):
        """Submit a previously uploaded file once, and wait for its own receipt.

        Must run off the event loop. A missing or uncertain receipt is unknown;
        it never authorizes automatic retry. The scheduler persists its intent
        before calling this method so a process restart cannot replay it.
        """
        if (
            not isinstance(artifact, PrintArtifact)
            or not isinstance(dispatch_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dispatch_id)
        ):
            raise AnycubicError("Invalid native print dispatch identity")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise AnycubicError("Invalid print acknowledgement timeout")
        options = validate_options(options)
        identity = (artifact.file.sha256, artifact.file.filename, json.dumps(options, sort_keys=True))
        with self._lock:
            previous = self._print_attempts.get(dispatch_id)
            if previous is not None:
                if previous["identity"] != identity:
                    raise AnycubicError("This print dispatch identity has already been used")
                return dict(previous["result"])
        sha256, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
        try:
            with artifact.file.path.open("rb") as stream:
                while chunk := stream.read(65536):
                    sha256.update(chunk)
                    md5.update(chunk)
                    size += len(chunk)
                    if size > artifact.file.size:
                        raise AnycubicError("The sliced file changed after upload; inspect it before retrying")
        except OSError:
            raise AnycubicError("The sliced file is unavailable; inspect it before retrying") from None
        if (size, sha256.hexdigest(), md5.hexdigest()) != (artifact.file.size, artifact.file.sha256, artifact.md5):
            raise AnycubicError("The sliced file changed after upload; inspect it before retrying")
        with self._lock:
            if dispatch_id in self._print_attempts:
                previous = self._print_attempts[dispatch_id]
                if previous["identity"] != identity:
                    raise AnycubicError("This print dispatch identity has already been used")
                return dict(previous["result"])
            capabilities = self.print_capabilities()
            if (
                not self.file_session_valid(context)
                or artifact.file.model != self.model
                or not capabilities["ready"]
                or cancelled is not None
                and cancelled.is_set()
            ):
                raise AnycubicError("The printer session changed or the printer is no longer idle")
            data = print_data(artifact, options, capabilities["ace_slots"], capabilities["options"])
            # Only settled attempts may be evicted. Unknown intents remain
            # remembered for this client's lifetime, as in the durable queue.
            if len(self._print_attempts) >= 256:
                for key, item in list(self._print_attempts.items()):
                    if item["result"]["status"] in {"accepted", "rejected"}:
                        self._print_attempts.pop(key)
                        break
                else:
                    raise AnycubicError("Inspect unresolved print submissions before creating another")
            result = {
                "status": "unknown",
                "task_id": None,
                "filename": artifact.file.filename,
                "msgid": str(uuid.uuid4()),
            }
            pending = {"identity": identity, "result": result, "event": threading.Event()}
            self._print_attempts[dispatch_id] = self._print_pending = pending
            queued = self._publish("print", "start", data, msgid=result["msgid"])
        try:
            if queued:
                pending["event"].wait(timeout)
            with self._lock:
                return dict(result)
        finally:
            with self._lock:
                if self._print_pending is pending:
                    self._print_pending = None

    def _control_report(self, kind, envelope):
        changed = False
        data = envelope.get("data")
        if envelope.get("code", 200) not in (0, 200) or not isinstance(data, dict):
            return False
        if kind in {"info", "print"}:
            # Native LAN reports the current profile in info/project and in
            # print/update settings as well as at the info root. A settings
            # report does not carry lifecycle telemetry, but must still reach
            # the status callback so the UI can show the observed profile.
            # Native info root overrides project metadata, which may be cached.
            sources = (data, data.get("project")) if kind == "info" else (data.get("settings"), data)
            for source in sources:
                mode = source.get("print_speed_mode") if isinstance(source, dict) else None
                if type(mode) is int and mode in (1, 2, 3):
                    self.state.speed_level = mode
                    changed = True
                    break
        if kind == "info":
            self._info_observed = time.monotonic()
            if isinstance(data.get("features"), dict):
                self._print_features = {
                    name: value is True or type(value) is int and value == 1
                    for name, value in data["features"].items()
                    if name in OPTION_FEATURES.values()
                }
                self._print_features_seen = self._info_observed
                changed = True
            for source in ("temp_limit", "free_temp_limit"):
                limits = data.get(source)
                if isinstance(limits, dict):
                    for key in ("nozzle_temp_limit", "hotbed_temp_limit"):
                        bounds = limits.get(key)
                        if (
                            isinstance(bounds, list)
                            and len(bounds) == 2
                            and type(bounds[1]) is int
                            and 0 < bounds[1] <= 400
                        ):
                            self._control_limits.setdefault(source, {})[key] = bounds[1]
                            changed = True
        if kind in {"fan", "info"}:
            for field in ("fan_speed_pct", "aux_fan_speed_pct", "box_fan_level"):
                value = data.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                    continue
                self._reported_fan_fields.add(field)
                changed = True
                if field != "box_fan_level":
                    self._reported_fans[field] = 100
                    changed = True
                else:
                    bounds = data.get("box_fan_level_limit")
                    if isinstance(bounds, list) and len(bounds) == 2 and bounds[1] in (3, 100):
                        self._reported_fans[field] = bounds[1]
                    elif value > 3:
                        self._reported_fans[field] = 100
                    # At 0..3 the scale is ambiguous. Keep an already observed
                    # scale, otherwise wait for a qualifying report.
                    scale = self._reported_fans.get(field)
                    if scale:
                        self.state.big_fan2_speed = round(value * 100 / scale)
                        changed = True
                    elif value == 0:
                        self.state.big_fan2_speed = 0
        if kind == "light":
            lights = data.get("lights", [data] if data.get("type") in (1, 2, 3) else [])
            if isinstance(lights, list):
                for light in lights:
                    if (
                        isinstance(light, dict)
                        and type(light.get("type")) is int
                        and light["type"] in (1, 2, 3)
                        and type(light.get("status")) is int
                        and light["status"] in (0, 1)
                    ):
                        self._reported_lights[light["type"]] = time.monotonic()
                        changed = True
        if (
            kind == "extrudeControl"
            and envelope.get("action") != "set"
            and type(data.get("operate_type")) is int
            and data["operate_type"] in (0, 1)
        ):
            self._extrude_observed = time.monotonic()
            self._extrude_operate = data["operate_type"]
            if self._extrude_operate == 0:
                self._extrusion_pending = False
            changed = True
        return changed

    def control_capabilities(self):
        model = self._model
        print_capabilities = self.print_capabilities()
        printing = self.state.state in {"RUNNING", "PAUSE"}
        limits = self._control_limits.get("temp_limit" if printing else "free_temp_limit", {})
        bed_max = model.bed_max if printing else (model.bed_idle_max or model.bed_max)
        return {
            "ready": self._controls_ready(),
            "nozzle_max": min(model.nozzle_max, limits.get("nozzle_temp_limit", model.nozzle_max)),
            "bed_max": min(bed_max, limits.get("hotbed_temp_limit", bed_max)),
            "fans": [
                name
                for name, field in (
                    ("part", "fan_speed_pct"),
                    ("aux", "aux_fan_speed_pct"),
                    ("chamber", "box_fan_level"),
                )
                if field in self._reported_fans
            ],
            "speed_modes": [1, 2, 3],
            "reported_fans": [
                name
                for name, field in (
                    ("part", "fan_speed_pct"),
                    ("aux", "aux_fan_speed_pct"),
                    ("chamber", "box_fan_level"),
                )
                if field in self._reported_fan_fields or field in self._reported_fans
            ],
            "ace_drying": self._ace_drying_capabilities(),
            "chamber_light": self._light_ready(),
            "motion": self._motion_ready(),
            "extruder_pulse": self._extrusion_ready(),
            "extrusion_stop": self._extrusion_stop_ready(),
            "ace_slots": self._ace_slot_capabilities(),
            "print_options": print_capabilities["options"],
            "print_ready": print_capabilities["ready"],
        }

    def _reset_manual_controls(self):
        if self._extrusion_timer is not None:
            self._extrusion_timer.cancel()
            self._extrusion_timer = None
        self._ace_seen.clear()
        self._ace_material_seen.clear()
        self._reported_lights.clear()
        self._info_observed = self._extrude_observed = 0.0
        self._extrude_operate = None
        self._extrusion_pending = False
        self._manual_feed = None

    def _fresh_connection(self):
        return self.state.connected and not self.is_stale and not self._stop.is_set()

    def ace_material_snapshot(self) -> list[dict] | None:
        """Return detached ACE inventory only after recent complete material reports.

        A one-slot or drying-only update must not renew the other slots' material
        authority. This read never requests status or sends a printer command.
        """
        with self._lock:
            if not self._fresh_connection() or self._client is None or not self._subscribed or not self._device_id:
                return None
            boxes = self._telemetry.boxes
            now = time.monotonic()
            if not boxes or any(
                box_id not in self._ace_material_seen
                or not 0 <= now - self._ace_material_seen[box_id] <= self.STALE_TIMEOUT
                or len(box.get("tray", [])) != 4
                or {tray.get("id") for tray in box["tray"]} != {0, 1, 2, 3}
                for box_id, box in boxes.items()
            ):
                return None
            return copy.deepcopy([boxes[identity] for identity in sorted(boxes)])

    def _light_ready(self):
        seen = self._reported_lights.get(2)
        return bool(self._fresh_connection() and seen and time.monotonic() - seen <= self.STALE_TIMEOUT)

    def _motion_ready(self):
        return bool(
            not self._extrusion_pending
            and self._extrusion_timer is None
            and self._extrude_operate != 1
            and self._controls_ready()
            and self.state.state == "IDLE"
            and self._info_observed
            and time.monotonic() - self._info_observed <= self.STALE_TIMEOUT
        )

    def set_chamber_light(self, on):
        with self._lock:
            if type(on) is not bool or not self._light_ready():
                return False
            return self._publish("light", "control", {"type": 2, "status": int(on), "brightness": 100 if on else 0})

    def jog_axis(self, axis, distance):
        with self._lock:
            if (
                not self._motion_ready()
                or axis not in ("x", "y", "z")
                or isinstance(distance, bool)
                or not isinstance(distance, (int, float))
                or not math.isfinite(distance)
                or not 0 < abs(distance) <= 50
            ):
                return False
            return self._publish(
                "axis",
                "move",
                {
                    "axis": {"x": 1, "y": 2, "z": 3}[axis],
                    "move_type": 1 if distance > 0 else 0,
                    "distance": abs(distance),
                },
            )

    def home_axes(self, axes="all"):
        with self._lock:
            if not self._motion_ready() or axes not in ("x", "y", "z", "xy", "all"):
                return False
            return self._publish(
                "axis",
                "move",
                {"axis": {"x": 1, "y": 2, "z": 3, "xy": 4, "all": 5}[axes], "move_type": 2, "distance": 0},
            )

    def _extrusion_ready(self):
        temperature = self.state.temperatures.get("nozzle")
        return bool(
            self._motion_ready()
            and self._extrude_observed
            and time.monotonic() - self._extrude_observed <= self.STALE_TIMEOUT
            and self._extrude_operate == 0
            and self._extrusion_timer is None
            and isinstance(temperature, (int, float))
            and temperature >= 170
        )

    def extruder_pulse(self, direction):
        """A bounded native pulse, not an inferred millimetre distance."""
        with self._lock:
            if type(direction) is not int or direction not in (0, 1) or not self._extrusion_ready():
                return False
            generation, connection = self._generation, self._client
            if not self._publish("extrudeControl", "set", {"operate_type": 1, "move_type": direction}):
                return False
            self._extrusion_pending = True
            self._extrude_operate = None

            def finish():
                with self._lock:
                    if self._extrusion_timer is not timer:
                        return
                    if self._current(generation, connection):
                        self._publish("extrudeControl", "set", {"operate_type": 0})
                        self._publish("extrudeControl", "getInfo", None)
                    self._extrusion_timer = None

            timer = self._extrusion_timer = threading.Timer(0.5, finish)
            self._extrusion_timer.daemon = True
            self._extrusion_timer.start()
            return True

    def _extrusion_stop_ready(self):
        return bool(
            self._fresh_connection()
            and (
                self._extrusion_pending
                or (
                    self.state.state == "IDLE"
                    and self._extrude_observed
                    and time.monotonic() - self._extrude_observed <= self.STALE_TIMEOUT
                )
            )
        )

    def stop_extrusion(self):
        with self._lock:
            if not self._extrusion_stop_ready():
                return False
            if self._extrusion_timer is not None:
                self._extrusion_timer.cancel()
                self._extrusion_timer = None
            queued = self._publish("extrudeControl", "set", {"operate_type": 0})
            if queued:
                self._publish("extrudeControl", "getInfo", None)
            return queued

    def _ace_slot_capabilities(self):
        controls = []
        for box in self._telemetry.boxes.values():
            fresh = bool(
                self._motion_ready()
                and box["id"] in self._ace_seen
                and time.monotonic() - self._ace_seen[box["id"]] <= self.STALE_TIMEOUT
            )
            for tray in box["tray"]:
                editable = tray.get("anycubic_edit_status") in (1, 2, 3) and tray.get("anycubic_status") != 4
                controls.append(
                    {
                        "box_id": box["id"],
                        "slot_id": tray["id"],
                        "configure": fresh and editable,
                        "feed": fresh and tray.get("state") != 9,
                        "feed_stop": bool(
                            self._fresh_connection()
                            and box["id"] in self._ace_seen
                            and time.monotonic() - self._ace_seen[box["id"]] <= self.STALE_TIMEOUT
                            and (
                                fresh
                                or (
                                    self._manual_feed == (box["id"], tray["id"])
                                    and self.state.state not in {"RUNNING", "PREPARE", "PAUSE"}
                                )
                            )
                        ),
                        "rfid_locked": tray.get("anycubic_edit_status") == 0,
                    }
                )
        return controls

    def set_ace_slot(self, box_id, slot_id, material, color):
        with self._lock:
            control = next(
                (
                    item
                    for item in self._ace_slot_capabilities()
                    if item["box_id"] == box_id and item["slot_id"] == slot_id
                ),
                None,
            )
            if (
                type(box_id) is not int
                or type(slot_id) is not int
                or not control
                or not control["configure"]
                or not isinstance(material, str)
                or not 1 <= len(material.strip()) <= 64
                or any(ord(c) < 32 for c in material)
                or not isinstance(color, str)
                or len(color) != 6
                or any(c not in "0123456789abcdefABCDEF" for c in color)
            ):
                return False
            rgb = [int(color[i : i + 2], 16) for i in (0, 2, 4)]
            queued = self._publish(
                "multiColorBox",
                "setInfo",
                {
                    "multi_color_box": [
                        {"id": box_id, "slots": [{"index": slot_id, "type": material.strip(), "color": rgb}]}
                    ]
                },
            )
            if queued:
                self._publish("multiColorBox", "getInfo", None)
            return queued

    def feed_ace_slot(self, box_id, slot_id, operation):
        with self._lock:
            control = next(
                (
                    item
                    for item in self._ace_slot_capabilities()
                    if item["box_id"] == box_id and item["slot_id"] == slot_id
                ),
                None,
            )
            if (
                type(box_id) is not int
                or type(slot_id) is not int
                or operation not in ("load", "unload", "stop")
                or not control
                or not control["feed_stop" if operation == "stop" else "feed"]
            ):
                return False
            queued = self._publish(
                "multiColorBox",
                "feedFilament",
                {
                    "multi_color_box": [
                        {
                            "id": box_id,
                            "feed_status": {
                                "slot_index": slot_id,
                                "type": {"load": 1, "unload": 2, "stop": 3}[operation],
                            },
                        }
                    ]
                },
            )
            if queued and operation != "stop":
                self._manual_feed = (box_id, slot_id)
            return queued

    def _observe_ace_drying(self, envelope):
        if envelope.get("action") == "getInfo":
            self._ace_drying_seen.clear()
            self._ace_seen.clear()
            self._ace_material_seen.clear()
        for box in envelope.get("data", {}).get("multi_color_box", []):
            if isinstance(box.get("slots"), list):
                self._ace_seen[box["id"]] = time.monotonic()
                slots = box["slots"]
                if (
                    len(slots) == 4
                    and {slot.get("index") for slot in slots} == {0, 1, 2, 3}
                    and all(
                        (type(slot.get("status")) is int and slot["status"] == 4)
                        or slot.get("_empty") is True
                        or (
                            type(slot.get("status")) is int
                            and type(slot.get("edit_status")) is int
                            and isinstance(slot.get("type"), str)
                            and len(slot["type"]) <= 64
                            and not any(ord(character) < 32 for character in slot["type"])
                            and isinstance(slot.get("color"), list)
                            and len(slot["color"]) in (3, 4)
                            and all(type(channel) is int and 0 <= channel <= 255 for channel in slot["color"])
                        )
                        for slot in slots
                    )
                ):
                    self._ace_material_seen[box["id"]] = time.monotonic()
                elif any({"type", "color", "status", "edit_status", "_empty"} & slot.keys() for slot in slots):
                    # A new type/RFID state with an old colour (or vice versa)
                    # is not a newly observed material tuple.
                    self._ace_material_seen.pop(box["id"], None)
            if isinstance(box.get("drying_status"), dict) and box["drying_status"]:
                self._ace_drying_seen[box["id"]] = time.monotonic()

    def _ace_drying_capabilities(self):
        controls = []
        now = time.monotonic()
        for box in self._telemetry.boxes.values():
            box_id = box["id"]
            if box_id < 0 or not (bounds := drying_limits(box)):
                continue
            fresh = bool(
                self.state.connected
                and not self.is_stale
                and not self._stop.is_set()
                and box_id in self._ace_drying_seen
                and now - self._ace_drying_seen.get(box_id, 0) <= self.STALE_TIMEOUT
                and box.get("anycubic_drying")
            )
            controls.append(
                {"id": box_id, **bounds, "start_ready": fresh and self._controls_ready(), "stop_ready": fresh}
            )
        return controls

    def set_ace_drying(self, box_id, *, temperature=None, duration_minutes=None, stop=False):
        """Queue one native setDry, then query authoritative box status."""
        with self._lock:
            if type(box_id) is not int or type(stop) is not bool:
                return False
            control = next((item for item in self._ace_drying_capabilities() if item["id"] == box_id), None)
            if control is None or not control["stop_ready" if stop else "start_ready"]:
                return False
            if stop:
                if temperature is not None or duration_minutes is not None:
                    return False
                drying = {"status": 0}
            else:
                if (
                    type(temperature) is not int
                    or not control["temp_min"] <= temperature <= control["temp_max"]
                    or type(duration_minutes) is not int
                    or not control["duration_min"] <= duration_minutes <= control["duration_max"]
                    or any(
                        temperature >= bound["temperature"] and duration_minutes > bound["max_minutes"]
                        for bound in control["duration_limits"]
                    )
                ):
                    return False
                drying = {"status": 1, "target_temp": temperature, "duration": duration_minutes}
            queued = self._publish(
                "multiColorBox", "setDry", {"multi_color_box": [{"id": box_id, "drying_status": drying}]}
            )
            if queued:
                self._publish("multiColorBox", "getInfo", None)
            return queued

    def _controls_ready(self):
        return bool(
            self.state.connected
            and not self.is_stale
            and not self._stop.is_set()
            and self.state.state in {"IDLE", "RUNNING", "PAUSE"}
            and self._last_state_time
            and time.monotonic() - self._last_state_time <= self.STALE_TIMEOUT
        )

    def _settings_update(self, settings):
        if self.state.state not in {"RUNNING", "PAUSE"} or not self._telemetry.task_id:
            return False
        return self._publish("print", "update", {"taskid": self._telemetry.task_id, "settings": settings})

    def _set_temperature(self, heater, target, nozzle=0):
        with self._lock:
            if (
                not self._controls_ready()
                or nozzle != 0
                or type(target) is not int
                or not 0 <= target <= self.control_capabilities()[f"{heater}_max"]
            ):
                return False
            field = "target_nozzle_temp" if heater == "nozzle" else "target_hotbed_temp"
            if self.state.state in {"RUNNING", "PAUSE"}:
                return self._settings_update({field: target})
            if self.state.state != "IDLE" or not self._last_state_time:
                return False
            return self._publish(
                "tempature",
                "set",
                {
                    "type": 0 if heater == "nozzle" else 1,
                    "target_nozzle_temp": target if heater == "nozzle" else 0,
                    "target_hotbed_temp": target if heater == "bed" else 0,
                },
            )

    def set_nozzle_temperature(self, target, nozzle=0):
        return self._set_temperature("nozzle", target, nozzle)

    def set_bed_temperature(self, target):
        return self._set_temperature("bed", target)

    def set_fan_percent(self, fan, percent):
        with self._lock:
            field = {"part": "fan_speed_pct", "aux": "aux_fan_speed_pct", "chamber": "box_fan_level"}.get(fan)
            if (
                not self._controls_ready()
                or field not in self._reported_fans
                or type(percent) is not int
                or not 0 <= percent <= 100
            ):
                return False
            value = round(percent * self._reported_fans[field] / 100)
            if self.state.state in {"RUNNING", "PAUSE"}:
                return self._settings_update({field: value})
            return (
                self.state.state == "IDLE"
                and bool(self._last_state_time)
                and self._publish("fan", "setSpeed", {field: value})
            )

    def set_print_speed(self, mode):
        with self._lock:
            if not self._controls_ready() or type(mode) is not int or mode not in (1, 2, 3):
                return False
            if self.state.state == "IDLE":
                queued = self._publish("print", "setSpeedMode", {"speed_mode": mode})
            else:
                queued = self._settings_update({"print_speed_mode": mode})
            if queued:
                # Read back the actual profile instead of optimistically
                # displaying an MQTT queue result as hardware acceptance.
                self._publish("info", "query", None)
            return queued

    def _reset_camera(self):
        self._camera_requested = False
        self._camera_url = self._camera_result = None
        self._camera_started_at = 0.0
        self._camera_event.set()

    def _camera_report(self, kind, envelope):
        if not self._camera_requested or kind not in {"video", "info"}:
            return
        data = envelope.get("data")
        state = envelope.get("state") or (data.get("state") if isinstance(data, dict) else None)
        if kind == "video" and (state == "pushStopped" or envelope.get("action") == "stopCapture"):
            return
        urls = data.get("urls") if isinstance(data, dict) else None
        if isinstance(urls, dict) and urls.get("rtspUrl"):
            self._camera_url = camera_stream_url(self.ip_address, urls["rtspUrl"])
        if kind == "video":
            if envelope.get("code", 200) not in (0, 200) or state in {"initFailed", "initFail", "error"}:
                self._camera_result = "failed"
            elif state == "initSuccess" or self._camera_url:
                self._camera_result = "ready"
        self._camera_event.set()

    def start_camera(self, timeout=8.0):
        """Camera-only activation, called off the event loop for an active viewer."""
        with self._camera_lock:
            with self._lock:
                if not self.state.connected or self.is_stale or self._stop.is_set():
                    raise AnycubicError("The Anycubic printer is not connected")
                if self._camera_url and time.monotonic() - self._camera_started_at < 20:
                    return self._camera_url
                generation = self._generation
                self._camera_requested = True
                self._camera_url = self._camera_result = None
                self._camera_event.clear()
            # New-generation camera firmware requires stop -> pause -> start.
            if self._model_id in {"20028", "20030"}:
                if not self._publish("video", "stopCapture", None) or self._stop.wait(1):
                    raise AnycubicError("Anycubic camera initialization failed")
            if not self._publish("video", "startCapture", None):
                raise AnycubicError("Anycubic camera initialization failed")
            self._publish("info", "query", None)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self._camera_event.wait(max(0, deadline - time.monotonic()))
                with self._lock:
                    if generation != self._generation or self._stop.is_set() or not self.state.connected:
                        raise AnycubicError("The Anycubic camera session ended")
                    if self._camera_result == "failed":
                        raise AnycubicError("The printer could not start its camera")
                    if self._camera_url or (
                        self._camera_result == "ready" and self._model_id not in {"20028", "20030"}
                    ):
                        self._camera_url = self._camera_url or camera_stream_url(self.ip_address)
                        self._camera_started_at = time.monotonic()
                        return self._camera_url
                    self._camera_event.clear()
            raise AnycubicError("The printer did not report a camera stream")

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
            self._reset_print_request()
            if self._extrusion_timer is not None:
                self._publish("extrudeControl", "set", {"operate_type": 0})
                self._extrusion_timer.cancel()
                self._extrusion_timer = None
            self._generation += 1
            self._stop.set()
            client, worker = self._client, self._worker
            self._client = None
            self._subscribed = False
            self._device_id = None
            self._reset_camera()
            self.state.connected = False
            self.state.state = "unknown"
            self.state.raw_data["ams"] = []
            self._telemetry.boxes = {}
            self._ace_material_seen.clear()
        if client is not None:
            retire_paho_client(client, "anycubic")
        if worker is not None and timeout > 0 and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def check_staleness(self) -> bool:
        """Clear stale status and return whether connected, as PrinterManager expects."""
        with self._lock:
            if not self.is_stale or not self.state.connected:
                return self.state.connected
            self.state.connected = False
            self.state.state = "unknown"
            self.state.raw_data["ams"] = []
            self._telemetry.boxes = {}
            self._ace_material_seen.clear()
        self._notify()
        return self.state.connected

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
    set_chamber_temperature = _unsupported
    set_fan_speed = set_part_fan = set_aux_fan = set_chamber_fan = _unsupported
    set_left_aux_fan = set_airduct_mode = select_extruder = _unsupported
    move_axis = disable_motors = enable_motors = _unsupported
    ams_load_filament = ams_unload_filament = ams_control = ams_set_filament_setting = _unsupported
    reset_ams_slot = extrusion_cali_sel = extrusion_cali_set = set_ams_filament_backup = _unsupported
    set_kprofile = set_kprofiles_batch = delete_kprofile = register_assignment_verification = _unsupported

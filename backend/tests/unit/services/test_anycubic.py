"""Offline protocol and lifecycle tests; never contact a printer or account."""

import base64
import copy
import hashlib
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.x509.oid import NameOID

from backend.app.services.anycubic import AnycubicError, AnycubicMQTTClient
from backend.app.services.anycubic.protocol import (
    PREFIX,
    Credentials,
    PinnedTLSContext,
    get_info,
    identity,
    pair,
    private_host,
)
from backend.app.services.anycubic.telemetry import Telemetry
from backend.app.utils.anycubic_models import MODELS

HOST = "192.168.1.50"
PIN = "a" * 64
TOKEN = "0123456789abcdefFEDCBA9876543210"
USN = "uuid:fdm:02-AA-BB-CC-DD-EE"


def info(**changes):
    return {
        "modelName": "Anycubic Kobra S1",
        "usn": USN,
        "token": TOKEN,
        "ctrlType": "lan",
        "ctrlInfoUrl": f"http://{HOST}:18910/ctrl",
        **changes,
    }


def encrypted(**changes):
    body = {
        "broker": f"mqtts://{HOST}:9883",
        "modeId": 20025,
        "deviceId": "device-one",
        "username": "secret-user",
        "password": "secret-password",
        **changes,
    }
    padder = PKCS7(128).padder()
    padded = padder.update(json.dumps(body).encode()) + padder.finalize()
    iv = b"0123456789abcdef"
    encryptor = Cipher(algorithms.AES(TOKEN[16:32].encode()), modes.CBC(iv)).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()
    return {"code": 200, "data": {"token": iv.decode(), "info": base64.b64encode(cipher).decode()}}


def report(kind, data, **changes):
    return {"type": kind, "action": "report", "code": 200, "data": data, **changes}


def box(identity=0, **changes):
    return {
        "id": identity,
        "model_id": 40002,
        "humidity": 0,
        "temp": 35,
        "slots": [
            {"index": i, "type": "PLA", "color": [20, 40, 60], "status": 5, "edit_status": 0, "consumables_percent": 0}
            for i in range(4)
        ],
        **changes,
    }


class BootstrapTests(unittest.TestCase):
    @patch("backend.app.services.anycubic.protocol._fetch")
    @patch("backend.app.services.anycubic.protocol.inspect_fingerprint", return_value=PIN)
    def test_supported_models_pair_using_their_own_model_id(self, inspect, fetch):
        for model in MODELS.values():
            with self.subTest(model=model.name):
                fetch.side_effect = [
                    info(modelName=f"Anycubic {model.name}", modelId=int(model.model_id)),
                    encrypted(modeId=int(model.model_id)),
                ]
                credentials = pair(HOST, USN, PIN)
                self.assertEqual(credentials.model_id, model.model_id)
                self.assertEqual(credentials.device_id, "device-one")

    @patch("backend.app.services.anycubic.client.inspect_fingerprint", return_value=PIN)
    @patch("backend.app.services.anycubic.client.get_info")
    def test_discovery_reports_actual_model_instead_of_s1(self, fetch, inspect):
        for model in MODELS.values():
            with self.subTest(model=model.name):
                fetch.return_value = info(modelName=f"Anycubic {model.name} Combo", modelId=model.model_id)
                discovered = AnycubicMQTTClient.discover(HOST)
                self.assertEqual(discovered["model"], model.name)
                self.assertEqual(discovered["model_id"], model.model_id)

    def test_private_numeric_addresses_only(self):
        self.assertEqual(private_host(HOST), HOST)
        for value in ("printer.local", "127.0.0.1", "169.254.169.254", "8.8.8.8", "224.0.0.1"):
            with self.subTest(value=value), self.assertRaises(AnycubicError):
                private_host(value)

    @patch("backend.app.services.anycubic.protocol._fetch")
    def test_original_s1_identity_without_cn_or_model_id(self, fetch):
        fetch.return_value = info()
        self.assertEqual(identity(get_info(HOST)), USN)
        fetch.assert_called_once_with(HOST, "GET", "/info")

    @patch("backend.app.services.anycubic.protocol._fetch")
    def test_modern_serial_and_model_id(self, fetch):
        fetch.return_value = info(cn="S1-SERIAL", modelId=20025)
        self.assertEqual(identity(get_info(HOST)), "S1-SERIAL")

    @patch("backend.app.services.anycubic.protocol._fetch")
    def test_refuses_cloud_unknown_model_and_foreign_endpoints(self, fetch):
        for value in (
            info(ctrlType="cloud"),
            info(modelId=20029),
            info(modelName="Anycubic Kobra 2 Max", modelId=20023),
            info(modelId=29999),
            info(modelName="Unknown"),
            info(ctrlInfoUrl="http://192.168.1.51:18910/ctrl"),
            info(ctrlInfoUrl=f"http://{HOST}:18910/reset"),
        ):
            fetch.return_value = value
            with self.subTest(value=value), self.assertRaises(AnycubicError):
                get_info(HOST)

    @patch("backend.app.services.anycubic.protocol._fetch")
    @patch("backend.app.services.anycubic.protocol.inspect_fingerprint", return_value=PIN)
    def test_pair_decrypts_credentials_and_preserves_mutual_tls_material(self, inspect, fetch):
        fetch.side_effect = [info(), encrypted(devicecrt="test-cert", devicepk="test-key")]
        credentials = pair(HOST, USN, PIN)
        self.assertEqual(credentials.device_id, "device-one")
        self.assertEqual(credentials.model_id, "20025")
        self.assertEqual(credentials.certificate, "test-cert")
        self.assertNotIn("secret", repr(credentials))
        self.assertTrue(fetch.call_args.args[2].startswith("/ctrl?ts="))

    @patch("backend.app.services.anycubic.protocol._fetch")
    @patch("backend.app.services.anycubic.protocol.inspect_fingerprint", return_value=PIN)
    def test_cryptojs_salted_formatter_and_known_device_id(self, inspect, fetch):
        response = encrypted()
        cipher = base64.b64decode(response["data"]["info"])
        response["data"]["info"] = base64.b64encode(b"Salted__" + b"12345678" + cipher).decode()
        fetch.side_effect = [info(), response]
        self.assertEqual(pair(HOST, "device-one", PIN).device_id, "device-one")

    @patch("backend.app.services.anycubic.protocol._fetch")
    @patch("backend.app.services.anycubic.protocol.inspect_fingerprint", return_value="b" * 64)
    def test_changed_pin_refused_before_pairing(self, inspect, fetch):
        with self.assertRaises(AnycubicError):
            pair(HOST, USN, PIN)
        fetch.assert_not_called()

    @patch("backend.app.services.anycubic.protocol._fetch")
    @patch("backend.app.services.anycubic.protocol.inspect_fingerprint", return_value=PIN)
    def test_rejects_broker_redirect_downgrade_wrong_identity_or_model(self, inspect, fetch):
        for changes in (
            {"broker": "mqtts://192.168.1.51:9883"},
            {"broker": f"mqtt://{HOST}:1883"},
            {"modeId": 20029},
            {"deviceId": "bad/topic"},
            {"devicepk": "key-without-cert"},
        ):
            fetch.side_effect = [info(), encrypted(**changes)]
            with self.subTest(changes=changes), self.assertRaises(AnycubicError):
                pair(HOST, USN, PIN)
        fetch.side_effect = [info(), encrypted()]
        with self.assertRaises(AnycubicError):
            pair(HOST, "unrelated-device", PIN)

    @patch("backend.app.services.anycubic.client.inspect_fingerprint", return_value=PIN)
    @patch("backend.app.services.anycubic.client.get_info", return_value=info())
    @patch("backend.app.services.anycubic.protocol.pair")
    def test_discovery_does_not_pair_or_return_secrets(self, pairing, get, inspect):
        result = AnycubicMQTTClient.discover(HOST)
        self.assertEqual(result["serial_number"], USN)
        self.assertEqual(result["tls_fingerprint"], PIN)
        self.assertNotIn("token", result)
        pairing.assert_not_called()


class TelemetryTests(unittest.TestCase):
    def test_complete_inventory_with_query_action_adds_units_without_erasing_siblings(self):
        telemetry = Telemetry("Kobra 3 V2")
        for box_id in (0, 1):
            telemetry.apply(
                "multiColorBox",
                report("multiColorBox", {"multi_color_box": [box(box_id)]}, action="query"),
            )
        self.assertEqual([unit["id"] for unit in telemetry.state.raw_data["ams"]], [0, 1])

    def test_x_builtin_feeder_and_external_units_keep_physical_addresses(self):
        from backend.app.services.printer_manager import printer_state_to_dict

        telemetry = Telemetry("Kobra X")
        telemetry.apply(
            "multiColorBox",
            report("multiColorBox", {"multi_color_box": [box(-1), box(0)]}, action="getInfo"),
        )
        units = telemetry.state.raw_data["ams"]
        self.assertEqual([unit["id"] for unit in units], [-1, 0])
        self.assertNotIn("humidity_raw", units[0])
        self.assertNotIn("temp", units[0])
        self.assertEqual(units[1]["temp"], 35)
        public = printer_state_to_dict(telemetry.state, model="Kobra X")["ams"]
        self.assertEqual([unit["id"] for unit in public], [-1, 0])
        self.assertTrue(all(len(unit["tray"]) == 4 for unit in public))

    def test_s1_max_projects_four_ace_units_and_s1_rejects_builtin_feeder(self):
        telemetry = Telemetry("Kobra S1 Max")
        telemetry.apply(
            "multiColorBox",
            report("multiColorBox", {"multi_color_box": [box(i) for i in range(4)]}, action="getInfo"),
        )
        self.assertEqual([unit["id"] for unit in telemetry.state.raw_data["ams"]], [0, 1, 2, 3])
        with self.assertRaises(AnycubicError):
            Telemetry().apply(
                "multiColorBox", report("multiColorBox", {"multi_color_box": [box(-1)]}, action="getInfo")
            )

    def setUp(self):
        self.telemetry = Telemetry()

    def full(self, boxes=None):
        return self.telemetry.apply(
            "multiColorBox",
            report("multiColorBox", {"multi_color_box": [box(), box(1)] if boxes is None else boxes}, action="getInfo"),
        )

    def test_two_ace_units_eight_distinct_slots_and_zero_values(self):
        self.assertTrue(self.full())
        units = self.telemetry.state.raw_data["ams"]
        self.assertEqual(
            [(u["id"], s["id"]) for u in units for s in u["tray"]],
            [(unit, slot) for unit in range(2) for slot in range(4)],
        )
        self.assertEqual(units[0]["humidity_raw"], 0)
        self.assertEqual(units[0]["tray"][0]["remain"], 0)
        self.assertEqual(units[0]["tray"][0]["tray_color"], "14283CFF")

    def test_partial_action_report_preserves_other_slots_and_units(self):
        self.full()
        self.telemetry.apply(
            "multiColorBox",
            report(
                "multiColorBox",
                {
                    "multi_color_box": [
                        {"id": 1, "slots": [{"index": 2, "color": [255, 0, 0]}], "drying_status": {"humidity": 25}}
                    ]
                },
            ),
        )
        units = self.telemetry.state.raw_data["ams"]
        self.assertEqual(len(units), 2)
        self.assertEqual(len(units[1]["tray"]), 4)
        self.assertEqual(units[1]["tray"][2]["tray_color"], "FF0000FF")
        self.assertEqual(units[1]["humidity_raw"], 25)

    def test_full_inventory_clears_no_longer_reported_remaining(self):
        self.full()
        value = box()
        for slot in value["slots"]:
            slot.pop("consumables_percent", None)
        self.full([value])
        self.assertNotIn("remain", self.telemetry.state.raw_data["ams"][0]["tray"][0])

    def test_status_four_clears_cached_material_even_when_firmware_repeats_type(self):
        self.full()
        self.telemetry.apply(
            "multiColorBox",
            report(
                "multiColorBox",
                {
                    "multi_color_box": [
                        {"id": 0, "slots": [{"index": 0, "status": 4, "type": "PLA", "color": [255, 0, 0]}]}
                    ]
                },
            ),
        )
        tray = self.telemetry.state.raw_data["ams"][0]["tray"][0]
        self.assertEqual(tray["state"], 9)
        self.assertEqual(tray["tray_type"], "")
        self.assertEqual(tray["tray_color"], "")
        self.assertNotIn("remain", tray)

    def test_unresolved_rfid_material_remains_unknown(self):
        value = box()
        value["slots"][0].update(edit_status=2)
        self.full([value])
        tray = self.telemetry.state.raw_data["ams"][0]["tray"][0]
        self.assertIsNone(tray["state"])
        self.assertEqual(tray["tray_type"], "")

    def test_full_snapshot_removes_missing_unit_and_incomplete_snapshot_is_atomic(self):
        self.full()
        self.full([box(1)])
        self.assertEqual([u["id"] for u in self.telemetry.state.raw_data["ams"]], [1])
        previous = copy.deepcopy(self.telemetry.state.raw_data)
        with self.assertRaises(AnycubicError):
            self.full([box(slots=[{"index": 0, "status": 4}])])
        self.assertEqual(self.telemetry.state.raw_data, previous)
        self.full([])
        self.assertEqual(self.telemetry.state.raw_data["ams"], [])

    def test_new_partial_unit_waits_for_full_inventory(self):
        self.assertFalse(
            self.telemetry.apply(
                "multiColorBox",
                report("multiColorBox", {"multi_color_box": [{"id": 1, "slots": [{"index": 0, "type": "PLA"}]}]}),
            )
        )
        self.assertEqual(self.telemetry.state.raw_data["ams"], [])

    def test_print_progress_and_temperature_pushes_merge_without_erasing_job(self):
        self.telemetry.apply(
            "info",
            report(
                "info",
                {
                    "state": "busy",
                    "project": {"state": "printing", "filename": "test.gcode", "progress": 25, "remain_time": 10},
                },
            ),
        )
        self.telemetry.apply("tempature", report("tempature", {"curr_nozzle_temp": 210, "target_hotbed_temp": 60}))
        state = self.telemetry.state
        self.assertEqual(state.state, "RUNNING")
        self.assertEqual(state.current_print, "test.gcode")
        self.assertEqual(state.remaining_time, 10)
        self.assertEqual(state.temperatures, {"nozzle": 210, "bed_target": 60})

    def test_idle_ignores_cached_finished_project_and_clears_current_job(self):
        self.telemetry.apply(
            "info",
            report(
                "info", {"state": "busy", "project": {"state": "printing", "filename": "old.gcode", "progress": 50}}
            ),
        )
        self.telemetry.apply(
            "info",
            report(
                "info", {"state": "free", "project": {"state": "printing", "filename": "old.gcode", "progress": 50}}
            ),
        )
        self.assertEqual(self.telemetry.state.state, "IDLE")
        self.assertIsNone(self.telemetry.state.current_print)

    def test_receipts_failures_urls_and_unknown_fields_are_not_state(self):
        self.assertFalse(self.telemetry.apply("print", report("print", {"taskid": "-1"})))
        self.assertFalse(self.telemetry.apply("print", report("print", {"progress": 50}, code=500)))
        self.telemetry.apply(
            "info",
            report(
                "info", {"state": "free", "urls": {"rtspUrl": "http://secret/?s=token"}, "password": "private-password"}
            ),
        )
        self.assertNotIn("secret", repr(self.telemetry.state.raw_data))
        self.assertNotIn("password", repr(self.telemetry.state.raw_data))


class ClientTests(unittest.TestCase):
    @patch("backend.app.services.anycubic.client.pair")
    @patch("backend.app.services.anycubic.client.mqtt.Client")
    def test_saved_model_mismatch_is_rejected_before_mqtt_credentials_are_used(self, mqtt_client, pairing):
        pairing.return_value = Credentials(HOST, "20029", "device-one", "user", "password")
        client = AnycubicMQTTClient(HOST, USN, model="Kobra 3", tls_fingerprint=PIN)
        client._run_session(0, client._stop)
        mqtt_client.assert_not_called()
        self.assertFalse(client.state.connected)

    def test_model_specific_subscriptions_publications_and_report_isolation(self):
        for model in MODELS.values():
            with self.subTest(model=model.name):
                client = AnycubicMQTTClient(HOST, USN, model=model.name, tls_fingerprint=PIN)
                mqtt = MagicMock()
                mqtt.publish.return_value.rc = 0
                client._client = mqtt
                client._device_id = "device-one"
                client._connected(0, mqtt, 0)
                mqtt.subscribe.assert_called_once_with(f"{PREFIX}/printer/public/{model.model_id}/device-one/#", qos=0)
                client._subscribed = True
                self.assertTrue(client._publish("info", "query", None))
                self.assertEqual(
                    mqtt.publish.call_args.args[0], f"{PREFIX}/web/printer/{model.model_id}/device-one/info"
                )
                payload = json.dumps(report("info", {"state": "free"})).encode()
                other_id = "20029" if model.model_id != "20029" else "20025"
                client._received(
                    0,
                    mqtt,
                    SimpleNamespace(
                        topic=f"{PREFIX}/printer/public/{other_id}/device-one/info/report",
                        payload=payload,
                    ),
                )
                self.assertFalse(client.state.connected)
                client._received(
                    0,
                    mqtt,
                    SimpleNamespace(
                        topic=f"{PREFIX}/printer/public/{model.model_id}/device-one/info/report",
                        payload=payload,
                    ),
                )
                self.assertTrue(client.state.connected)
                self.assertEqual(client.state.raw_data["device_model"], model.name)

    def setUp(self):
        self.callback = MagicMock()
        self.forbidden_callback = MagicMock()
        self.client = AnycubicMQTTClient(
            HOST,
            USN,
            on_state_change=self.callback,
            tls_fingerprint=PIN,
            on_print_start=self.forbidden_callback,
            on_ams_change=self.forbidden_callback,
        )
        self.mqtt = MagicMock()
        self.mqtt.publish.return_value.rc = 0
        self.client._client = self.mqtt
        self.client._device_id = "device-one"
        self.client._subscribed = True

    def receive(self, kind, data, **changes):
        message = SimpleNamespace(
            topic=f"{PREFIX}/printer/public/20025/device-one/{kind}/report",
            payload=json.dumps(report(kind, data, **changes)).encode(),
        )
        self.client._received(0, self.mqtt, message)

    def test_connack_and_command_receipts_are_not_connection_readiness(self):
        self.client._connected(0, self.mqtt, 0)
        self.assertFalse(self.client.state.connected)
        self.receive("print", {"taskid": "-1"})
        self.assertFalse(self.client.state.connected)
        self.receive("info", {"state": "free"})
        self.assertTrue(self.client.state.connected)

    def test_old_generation_wrong_device_and_oversize_payload_are_ignored(self):
        for generation, device, payload in (
            (1, "device-one", b"{}"),
            (0, "other-device", b"{}"),
            (0, "device-one", b"x" * 262145),
        ):
            message = SimpleNamespace(topic=f"{PREFIX}/printer/public/20025/{device}/info/report", payload=payload)
            self.client._received(generation, self.mqtt, message)
        self.assertEqual(self.client.report_messages_since_connect, 0)

    def test_only_status_callback_runs_and_receives_detached_snapshot(self):
        self.receive("info", {"state": "busy", "project": {"state": "printing", "filename": "test.gcode"}})
        self.assertTrue(self.callback.called)
        self.forbidden_callback.assert_not_called()
        self.assertIsNot(self.callback.call_args.args[0], self.client.state)

    def test_pause_resume_stop_exact_wire_and_no_optimistic_state_change(self):
        self.receive("info", {"state": "busy", "project": {"state": "printing", "taskid": "current-task"}})
        self.assertTrue(self.client.pause_print())
        args, kwargs = self.mqtt.publish.call_args
        self.assertEqual(args[0], f"{PREFIX}/web/printer/20025/device-one/print")
        body = json.loads(args[1])
        self.assertEqual((body["action"], body["data"]), ("pause", {"taskid": "current-task"}))
        self.assertGreater(body["timestamp"], 0)
        self.assertFalse(kwargs["retain"])
        self.assertEqual(self.client.state.state, "RUNNING")
        self.assertFalse(self.client.resume_print())
        self.receive("info", {"state": "busy", "project": {"state": "paused"}})
        self.assertTrue(self.client.resume_print())
        self.assertTrue(self.client.stop_print())

    def test_stale_lifecycle_cannot_be_refreshed_by_temperature_push_for_controls(self):
        self.receive("info", {"state": "busy", "project": {"state": "printing"}})
        self.client._last_state_time = time.monotonic() - 31
        self.receive("tempature", {"curr_nozzle_temp": 210})
        self.assertFalse(self.client.stop_print())

    def test_staleness_clears_material_cache_and_controls(self):
        self.receive("multiColorBox", {"multi_color_box": [box(), box(1)]}, action="getInfo")
        self.client._last_message_time = time.time() - 61
        self.client._last_message_monotonic = time.monotonic() - 61
        self.assertTrue(self.client.check_staleness())
        self.assertFalse(self.client.state.connected)
        self.assertEqual(self.client.state.raw_data["ams"], [])
        self.assertEqual(self.client._telemetry.boxes, {})
        self.assertFalse(self.client.pause_print())

    def test_partial_info_and_progress_do_not_renew_lifecycle_authority(self):
        self.receive("info", {"state": "busy", "project": {"state": "printing"}})
        self.client._last_state_time = time.monotonic() - 31
        previous = self.client._last_state_time
        self.receive("print", {"progress": 42})
        self.assertEqual(self.client._last_state_time, previous)
        self.assertFalse(self.client.pause_print())
        self.receive("info", {"state": "busy", "temp": {"curr_nozzle_temp": 210}})
        self.assertEqual(self.client._last_state_time, previous)
        self.assertEqual(self.client.state.state, "BUSY")

    def test_print_phase_inside_data_and_pause_flags_are_authoritative(self):
        self.receive("print", {"state": "printing", "progress": 42})
        self.assertTrue(self.client.pause_print())
        self.receive("info", {"state": "busy", "project": {"pause": 1}})
        self.assertEqual(self.client.state.state, "PAUSE")
        self.assertTrue(self.client.resume_print())

    def test_pausing_and_stopping_are_not_resume_authority(self):
        for phase in ("pausing", "stopping"):
            self.receive("info", {"state": "busy", "project": {"state": phase}})
            self.assertEqual(self.client.state.state, "PAUSE")
            self.assertFalse(self.client.resume_print())

    def test_retained_and_repeated_report_ids_cannot_refresh_authority(self):
        body = report("info", {"state": "busy", "project": {"state": "printing"}}, msgid="fixture-report")
        message = SimpleNamespace(
            topic=f"{PREFIX}/printer/public/20025/device-one/info/report",
            payload=json.dumps(body).encode(),
            retain=True,
        )
        self.client._received(0, self.mqtt, message)
        self.assertFalse(self.client.state.connected)
        message.retain = False
        self.client._received(0, self.mqtt, message)
        self.client._last_state_time = time.monotonic() - 31
        self.client._received(0, self.mqtt, message)
        self.assertFalse(self.client.pause_print())
        self.assertEqual(self.client.report_messages_since_connect, 1)

    def test_wall_clock_changes_cannot_extend_control_or_staleness_window(self):
        self.receive("info", {"state": "busy", "project": {"state": "printing"}})
        self.client._last_state_time = time.monotonic() - 31
        self.client._last_message_monotonic = time.monotonic() - 61
        with patch("time.time", return_value=time.time() - 86400):
            self.assertTrue(self.client.is_stale)
            self.assertFalse(self.client.stop_print())

    def test_explicit_repeated_controls_use_distinct_ids(self):
        self.receive("info", {"state": "busy", "project": {"state": "printing"}})
        self.assertTrue(self.client.pause_print())
        self.assertTrue(self.client.pause_print())
        ids = [json.loads(call.args[1])["msgid"] for call in self.mqtt.publish.call_args_list]
        self.assertEqual(len(ids), len(set(ids)))

    def test_bambu_specific_operations_never_publish(self):
        for method, args in (
            ("start_print", ("anything",)),
            ("send_gcode", ("G28",)),
            ("ams_load_filament", (0,)),
            ("send_command", ({"print": {}},)),
            ("set_bed_temperature", (60,)),
            ("publish_raw", ("topic", "payload")),
        ):
            self.assertFalse(getattr(self.client, method)(*args))
        self.mqtt.publish.assert_not_called()

    def test_poll_uses_getinfo_for_ace_and_actual_temperature_spelling(self):
        self.assertTrue(self.client.request_status_update())
        bodies = [json.loads(call.args[1]) for call in self.mqtt.publish.call_args_list]
        self.assertIn(("multiColorBox", "getInfo"), [(b["type"], b["action"]) for b in bodies])
        self.assertIn("tempature", [b["type"] for b in bodies])

    def test_supervisor_rebootstraps_with_bounded_backoff_without_replaying_controls(self):
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.side_effect = [False, True]
        with patch.object(self.client, "_run_session") as session:
            self.client._run(0, stop)
        self.assertEqual(session.call_count, 2)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [2, 4])
        self.mqtt.publish.assert_not_called()

    @patch("backend.app.services.anycubic.client.retire_paho_client")
    def test_disconnect_invalidates_late_callbacks(self, retire):
        self.client.disconnect()
        self.receive("info", {"state": "free"})
        self.assertFalse(self.client.state.connected)
        self.assertEqual(self.client.report_messages_since_connect, 0)
        retire.assert_called_once()


class TLSPinningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic printer")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        cls.cert = cert.public_bytes(serialization.Encoding.PEM)
        cls.key = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        cls.pin = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    def exchange(self, pin, implicit=False):
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            cert_path.write_bytes(self.cert)
            key_path.write_bytes(self.key)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)

            def serve():
                try:
                    raw, _ = listener.accept()
                    raw.settimeout(3)
                    with context.wrap_socket(raw, server_side=True) as secured:
                        seen.append(secured.recv(64))
                        if seen[-1]:
                            secured.sendall(b"ok")
                except OSError:
                    seen.append(b"")
                finally:
                    listener.close()

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            error = None
            try:
                with (
                    socket.create_connection(listener.getsockname(), timeout=3) as raw,
                    PinnedTLSContext(pin, HOST).wrap_socket(
                        raw, server_hostname=HOST, do_handshake_on_connect=not implicit
                    ) as secured,
                ):
                    secured.sendall(b"synthetic-MQTT-credentials")
                    self.assertEqual(secured.recv(2), b"ok")
            except ssl.SSLCertVerificationError as caught:
                error = caught
            worker.join(4)
            self.assertFalse(worker.is_alive())
        return seen, error

    def test_matching_pin_authenticates_before_first_write(self):
        seen, error = self.exchange(self.pin, implicit=True)
        self.assertIsNone(error)
        self.assertEqual(seen, [b"synthetic-MQTT-credentials"])

    def test_mismatched_pin_sends_no_application_credentials(self):
        seen, error = self.exchange("0" * 64)
        self.assertIsInstance(error, ssl.SSLCertVerificationError)
        self.assertTrue(all(not value for value in seen))

    def test_bound_host_rejects_different_destination(self):
        with socket.socket() as raw, self.assertRaises(ssl.SSLCertVerificationError):
            PinnedTLSContext(self.pin, HOST).wrap_socket(raw, server_hostname="192.168.1.51")

    def test_mutual_tls_material_loads_from_private_temporary_files_and_is_removed(self):
        credentials = Credentials(
            HOST, "20025", "device-one", "user", "password", self.cert.decode(), self.key.decode()
        )
        with tempfile.TemporaryDirectory() as directory, patch("tempfile.tempdir", directory):
            context = PinnedTLSContext(self.pin, HOST, credentials)
            self.assertEqual(context.bound_host, HOST)
            self.assertEqual(list(Path(directory).iterdir()), [])

"""Small allowlisted projection into Bambuddy's internal display state.

ACE addresses remain (box id, slot index). The ``ams`` display projection is
never sent on a Bambu wire protocol or used to trigger Bambu archive/spool hooks.
"""

import copy
import math

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.utils.anycubic_models import get_model

from .drying import status_fields
from .protocol import AnycubicError

_PHASES = {
    "printing": "RUNNING",
    "resumed": "RUNNING",
    "resuming": "RUNNING",
    "paused": "PAUSE",
    "pausing": "PAUSE",
    "finished": "FINISH",
    "stoped": "FAILED",
    "stopped": "FAILED",
    "failed": "FAILED",
    "preheating": "PREPARE",
    "auto_leveling": "PREPARE",
    "vibrating": "PREPARE",
    "flow_calibrating": "PREPARE",
    "downloading": "PREPARE",
    "checking": "PREPARE",
    "stopping": "PAUSE",
}
_TEMPS = {
    "curr_nozzle_temp": "nozzle",
    "target_nozzle_temp": "nozzle_target",
    "curr_hotbed_temp": "bed",
    "target_hotbed_temp": "bed_target",
    "curr_chamber_temp": "chamber",
    "target_chamber_temp": "chamber_target",
}


def number(value, low=0, high=1000000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if low <= value <= high else None


def _text(value, limit=512):
    if isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 for c in value):
        return value
    return None


class Telemetry:
    def __init__(self, model="Kobra S1"):
        selected_model = get_model(model)
        if selected_model is None:
            raise AnycubicError("Unsupported Anycubic LAN printer model")
        self._ace_ids = selected_model.ace_ids
        self.state = PrinterState()
        self.state.raw_data = {"manufacturer": "anycubic", "device_model": selected_model.name, "ams": []}
        self.boxes = {}
        self.task_id = "-1"  # Firmware's documented current-job command sentinel.
        self.phase = None
        self.lifecycle_observed = False

    def _temperatures(self, data):
        changed = False
        for source, destination in _TEMPS.items():
            value = number(data.get(source), -30, 400)
            if value is not None:
                self.state.temperatures[destination] = value
                changed = True
        return changed

    def _project(self, data, phase=None):
        changed = False
        phase = data.get("state", phase)
        if isinstance(phase, str) and phase.lower() in _PHASES:
            self.phase = phase.lower()
            self.state.state = _PHASES[self.phase]
            self.lifecycle_observed = True
            changed = True
        pause = data.get("pause")
        if type(pause) is int and pause in (1, 2, 3, 4):
            self.phase = {1: "paused", 2: "pausing", 3: "resuming", 4: "stopping"}[pause]
            self.state.state = _PHASES[self.phase]
            self.lifecycle_observed = True
            changed = True
        for source, attr, maximum in (
            ("progress", "progress", 100),
            ("curr_layer", "layer_num", 1000000),
            ("total_layers", "total_layers", 1000000),
            ("remain_time", "remaining_time", 1000000),
        ):
            value = number(data.get(source), 0, maximum)
            if value is not None:
                setattr(self.state, attr, value if attr == "progress" else int(value))
                changed = True
        name = _text(data.get("filename"))
        if name is not None:
            self.state.current_print = self.state.subtask_name = name
            self.state.gcode_file = name
            changed = True
        task = data.get("taskid", data.get("task_id"))
        if type(task) is int and 0 < task <= (1 << 63) - 1:
            task = str(task)
        task = _text(task, 128)
        if task:
            self.task_id = task
            self.state.subtask_id = task
        return changed

    def _materials(self, data, full):
        raw_boxes = data.get("multi_color_box")
        if not isinstance(raw_boxes, list) or len(raw_boxes) > len(self._ace_ids):
            return False
        updated = {} if full else copy.deepcopy(self.boxes)
        seen = set()
        for raw in raw_boxes:
            if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] not in self._ace_ids:
                raise AnycubicError("Unsupported ACE unit address")
            identity = raw["id"]
            if identity in seen:
                raise AnycubicError("Duplicate ACE unit address")
            seen.add(identity)
            new_unit = identity not in updated
            if not full and new_unit:
                # Wait for getInfo before projecting a newly seen unit. A partial
                # unit would be mistaken for a single-slot Bambu dryer downstream.
                # Other firmware labels complete inventories query/refresh instead.
                slots = raw.get("slots")
                if (
                    not isinstance(slots, list)
                    or len(slots) != 4
                    or any(not isinstance(slot, dict) or type(slot.get("index")) is not int for slot in slots)
                    or {slot["index"] for slot in slots} != {0, 1, 2, 3}
                ):
                    return False
            unit_full = full or new_unit
            box = updated.setdefault(identity, {"id": identity, "tray": []})
            if "slots" in raw:
                if not isinstance(raw["slots"], list) or len(raw["slots"]) > 4:
                    raise AnycubicError("Invalid ACE slots")
                trays = {} if unit_full else {t["id"]: t for t in box["tray"]}
                indexes = set()
                for slot in raw["slots"]:
                    if (
                        not isinstance(slot, dict)
                        or type(slot.get("index")) is not int
                        or slot["index"] not in range(4)
                    ):
                        raise AnycubicError("Unsupported ACE slot address")
                    index = slot["index"]
                    if index in indexes:
                        raise AnycubicError("Duplicate ACE slot address")
                    indexes.add(index)
                    tray = trays.setdefault(index, {"id": index, "state": None})
                    material = _text(slot.get("type"), 64)
                    if type(slot.get("status")) is int:
                        tray["anycubic_status"] = slot["status"]
                    if type(slot.get("edit_status")) is int:
                        tray["anycubic_edit_status"] = slot["edit_status"]
                    status, edit = tray.get("anycubic_status"), tray.get("anycubic_edit_status")
                    if status == 4 or slot.get("_empty") is True:
                        tray.clear()
                        tray.update(id=index, tray_type="", tray_color="", state=9, anycubic_status=4)
                    else:
                        if material is not None:
                            if material.strip().upper() in {"", "?", "UNKNOWN", "UNKONWN", "DEFAULT"}:
                                tray.update(tray_type="", tray_color="")
                                tray.pop("remain", None)
                            else:
                                tray["tray_type"] = material.strip()
                        if edit == 2 and status == 5:
                            tray.update(tray_type="", tray_color="")
                            tray.pop("remain", None)
                        occupied = tray.get("tray_type") and (status == 5 or edit in (0, 1, 3))
                        tray["state"] = 3 if occupied else None
                    color = slot.get("color")
                    if tray.get("tray_type") and isinstance(color, list) and len(color) in (3, 4):
                        if all(type(c) is int and 0 <= c <= 255 for c in color):
                            tray["tray_color"] = "".join(f"{c:02X}" for c in color[:3]) + "FF"
                    remaining = number(slot.get("consumables_percent"), 0, 100)
                    if remaining is not None and tray.get("tray_type"):
                        tray["remain"] = remaining
                    elif unit_full:
                        tray.pop("remain", None)
                if unit_full and indexes != {0, 1, 2, 3}:
                    raise AnycubicError("Incomplete ACE snapshot")
                box["tray"] = [trays[i] for i in sorted(trays)]
            elif unit_full:
                raise AnycubicError("Incomplete ACE snapshot")
            # The X's negative-ID feeder has no dry box or humidity sensor.
            raw_drying_status = raw.get("drying_status") if identity >= 0 else None
            dry = raw_drying_status if isinstance(raw_drying_status, dict) else {}
            if identity >= 0 and dry:
                box["anycubic_drying"] = status_fields(dry, None if unit_full else box.get("anycubic_drying"))
            elif unit_full:
                box.pop("anycubic_drying", None)
            humidity = raw.get("humidity") if identity >= 0 else None
            if humidity is None:
                humidity = dry.get("humidity")
            for key, value, maximum in (
                ("humidity_raw", humidity, 100),
                ("temp", raw.get("temp") if identity >= 0 else None, 100),
                ("drying_temp", dry.get("target_temp"), 100),
                ("drying_time", dry.get("remain_time"), 100000),
            ):
                value = number(value, 0, maximum)
                if value is not None:
                    box[key] = value
            if type(dry.get("status")) is int:
                box["anycubic_drying_status"] = dry["status"]
            if type(raw.get("loaded_slot")) is int and -1 <= raw["loaded_slot"] <= 3:
                box["loaded_slot"] = raw["loaded_slot"]
            if type(raw.get("model_id")) is int:
                box["anycubic_model_id"] = raw["model_id"]
            for source, target in (("sn", "serial_number"), ("version", "sw_ver")):
                if (text := _text(raw.get(source), 128)) is not None:
                    box[target] = text
        self.boxes = updated
        self.state.raw_data["ams"] = [copy.deepcopy(updated[i]) for i in sorted(updated)]
        return True

    def apply(self, kind, envelope):
        """Return true only for usable telemetry, never command receipts or errors."""
        self.lifecycle_observed = False
        data = envelope.get("data")
        if envelope.get("code", 200) not in (0, 200) or not isinstance(data, dict):
            return False
        if kind == "info":
            changed = False
            if isinstance(data.get("temp"), dict):
                changed = self._temperatures(data["temp"])
            if data.get("state") == "free":
                self.state.state = "IDLE"
                self.phase = None
                self.lifecycle_observed = True
                self.state.current_print = self.state.subtask_name = self.state.gcode_file = None
                self.state.subtask_id = None
                self.state.progress = self.state.remaining_time = self.state.layer_num = self.state.total_layers = 0
                changed = True
            elif data.get("state") == "busy":
                if isinstance(data.get("project"), dict):
                    changed = self._project(data["project"]) or changed
                if not self.lifecycle_observed:
                    self.state.state = "BUSY"
                    self.phase = None
                changed = True
            version = _text(data.get("version"), 128)
            if version:
                self.state.firmware_version = version
            return changed
        if kind == "tempature":
            return self._temperatures(data)
        if kind == "print":
            if (
                "progress" in data
                or str(envelope.get("state", "")).lower() in _PHASES
                or str(data.get("state", "")).lower() in _PHASES
                or (type(data.get("pause")) is int and data["pause"] in (1, 2, 3, 4))
            ):
                return self._project(data, envelope.get("state"))
            return False
        if kind == "multiColorBox":
            if envelope.get("action") == "setDry":
                # A setting receipt is not an independently observed cycle.
                return False
            return self._materials(data, envelope.get("action") == "getInfo")
        if kind == "fan":
            changed = False
            for key, attr in (("fan_speed_pct", "cooling_fan_speed"), ("aux_fan_speed_pct", "big_fan1_speed")):
                value = number(data.get(key), 0, 100)
                if value is not None:
                    setattr(self.state, attr, int(value))
                    changed = True
            return changed
        if kind == "light":
            lights = data.get("lights", [data] if data.get("type") == 2 else [])
            if isinstance(lights, list):
                for light in lights:
                    if isinstance(light, dict) and light.get("type") == 2 and type(light.get("status")) is int:
                        self.state.chamber_light = light["status"] == 1
                        return True
        return False

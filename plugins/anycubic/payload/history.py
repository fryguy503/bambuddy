"""Bounded package-owned ACE sensor history; no vendor schema in core."""

from datetime import datetime, timedelta, timezone

from .persistence import printer_key, read_json, state_root, storage_lock, write_json


def record(config, boxes):
    now = datetime.now(timezone.utc)
    key = printer_key(config)
    with storage_lock("history:" + key):
        path = state_root() / "history" / (key + ".json")
        value = read_json(path, {"format_version": 1, "rows": [], "last": {}})
        changed = False
        for box in boxes:
            identity = box.get("id")
            if type(identity) is not int or identity < 0:
                continue
            humidity, temperature = box.get("humidity_raw"), box.get("temp")
            if humidity is None and temperature is None:
                continue
            last = value["last"].get(str(identity))
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 300:
                continue
            timestamp = now.isoformat()
            value["rows"].append(
                {"unit_id": identity, "timestamp": timestamp, "humidity": humidity, "temperature": temperature}
            )
            value["last"][str(identity)] = timestamp
            changed = True
        if changed:
            cutoff = (now - timedelta(days=14)).isoformat()
            value["rows"] = [row for row in value["rows"] if row["timestamp"] >= cutoff][-30000:]
            write_json(path, value)


def readings(config, box, hours=24):
    key = printer_key(config)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with storage_lock("history:" + key):
        value = read_json(state_root() / "history" / (key + ".json"), {"format_version": 1, "rows": [], "last": {}})
        return [row for row in value["rows"] if row["unit_id"] == box and row["timestamp"] >= cutoff]

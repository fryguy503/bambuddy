"""Durable package-owned dispatch intents and native artifact metadata.

A corrupt journal fails closed. No network command belongs inside this module.
Records survive plugin upgrades because storage is outside the version payload.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_thread_lock = threading.RLock()
_MAX_BYTES = 8 * 1024 * 1024
_PENDING = {"intent", "submitting", "publishing", "unknown", "accepted", "running"}
_OUTCOMES = {"completed", "failed", "cancelled", "aborted"}
_STATUSES = _PENDING | {
    "completed",
    "failed",
    "cancelled",
    "aborted",
    "rejected",
    "resolved",
    "plate_required",
    "metadata",
}


class JournalError(ValueError):
    pass


def printer_key(config) -> str:
    return hashlib.sha256(config.serial_number.encode()).hexdigest()[:24]


def state_root() -> Path:
    from backend.app.core.config import settings

    root = Path(settings.base_dir) / "printer-plugin-state" / "anycubic"
    if os.name == "nt":
        absolute = str(root.absolute())
        if not absolute.startswith("\\\\?\\"):
            absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
        return Path(absolute)
    return root


def _digest(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise JournalError("Invalid plugin storage identity")
    return hashlib.sha256(value.encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate plugin state field")
        result[key] = value
    return result


def _invalid_number(_value):
    raise ValueError("Non-finite plugin state number")


def read_json(path: Path, default):
    if path.is_symlink():
        raise JournalError("Plugin state must not be a symbolic link")
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_BYTES:
            raise JournalError("Plugin state exceeds its storage boundary")
        value = json.loads(path.read_bytes(), object_pairs_hook=_unique_object, parse_constant=_invalid_number)
        if not isinstance(value, dict) or type(value.get("format_version")) is not int or value["format_version"] != 1:
            raise JournalError("Unsupported plugin state format")
        return value
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise JournalError("Plugin state is unreadable; retain it for recovery") from exc


def write_json(path: Path, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > _MAX_BYTES:
        raise JournalError("Plugin state exceeds its storage boundary")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise JournalError("Plugin state must not be a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def storage_lock(name: str):
    """Coordinate native callback threads and independent application processes."""
    with _thread_lock:
        directory = state_root() / "locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (_digest(name) + ".lock")
        if path.is_symlink():
            raise JournalError("Plugin state lock must not be a symbolic link")
        with path.open("a+b") as stream:
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + 5
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError) as exc:
                    if time.monotonic() >= deadline:
                        raise JournalError("Plugin state is busy; no command was sent") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Journal:
    def __init__(self, printer_key: str):
        self.printer_key = printer_key
        self.directory = state_root() / "printers" / _digest(printer_key)

    def _path(self, job_id):
        return self.directory / "jobs" / (_digest(job_id) + ".json")

    def _record(self, value, job_id=None):
        record = value.get("record")
        if not isinstance(record, dict):
            raise JournalError("Dispatch journal record is invalid")
        identity = record.get("job_id")
        if not isinstance(identity, str):
            raise JournalError("Dispatch journal identity is invalid")
        _digest(identity)
        if (
            job_id is not None
            and identity != job_id
            or record.get("printer_key") != self.printer_key
            or not isinstance(record.get("status"), str)
            or record.get("status") not in _STATUSES
        ):
            raise JournalError("Dispatch journal identity does not match")
        pending = record.get("pending_outcome")
        if pending is not None and (not isinstance(pending, str) or pending not in _OUTCOMES):
            raise JournalError("Dispatch journal accounting outcome is invalid")
        return record

    def get(self, job_id):
        with storage_lock(self.printer_key):
            path = self._path(job_id)
            value = read_json(path, {"format_version": 1})
            return self._record(value, job_id) if path.exists() else None

    def begin(self, job_id, record) -> bool:
        with storage_lock(self.printer_key):
            path = self._path(job_id)
            if path.exists():
                self._record(read_json(path, {}), job_id)
                return False
            now = datetime.now(timezone.utc).isoformat()
            value = {**record, "job_id": job_id, "printer_key": self.printer_key, "created_at": now, "updated_at": now}
            value.setdefault("status", "intent")
            self._record({"record": value}, job_id)
            write_json(path, {"format_version": 1, "record": value})
            return True

    def update(self, job_id, **fields):
        with storage_lock(self.printer_key):
            path = self._path(job_id)
            value = read_json(path, {"format_version": 1})
            record = self._record(value, job_id)
            record.update(fields)
            record.update(
                job_id=job_id, printer_key=self.printer_key, updated_at=datetime.now(timezone.utc).isoformat()
            )
            self._record(value, job_id)
            write_json(path, value)
            return dict(record)

    def unresolved(self, printer_key=None):
        if printer_key is not None and printer_key != self.printer_key:
            return Journal(printer_key).unresolved()
        with storage_lock(self.printer_key):
            result = []
            for path in sorted((self.directory / "jobs").glob("*.json")):
                record = self._record(read_json(path, {}))
                if self._path(record["job_id"]) != path:
                    raise JournalError("Dispatch journal filename identity does not match")
                if record.get("status") in _PENDING:
                    result.append(record)
            return result

    def artifact(self, printer_key, uri):
        with storage_lock(printer_key):
            path = state_root() / "artifacts" / _digest(printer_key) / (_digest(uri) + ".json")
            value = read_json(path, {"format_version": 1})
            if not path.exists():
                return None
            if (
                value.get("uri") != uri
                or value.get("printer_key", printer_key) != printer_key
                or not isinstance(value.get("record"), dict)
            ):
                raise JournalError("Artifact journal identity does not match")
            return value["record"]

    def pending_accounting(self):
        """Terminal hardware records awaiting accounting, never command replay."""
        with storage_lock(self.printer_key):
            result = []
            for path in sorted((self.directory / "jobs").glob("*.json")):
                record = self._record(read_json(path, {}))
                if self._path(record["job_id"]) != path:
                    raise JournalError("Dispatch journal filename identity does not match")
                pending = record.get("pending_outcome")
                if pending in _OUTCOMES and record["status"] == pending:
                    result.append(record)
            return result

    def save_artifact(self, printer_key, uri, record):
        with storage_lock(printer_key):
            path = state_root() / "artifacts" / _digest(printer_key) / (_digest(uri) + ".json")
            write_json(path, {"format_version": 1, "printer_key": printer_key, "uri": uri, "record": record})

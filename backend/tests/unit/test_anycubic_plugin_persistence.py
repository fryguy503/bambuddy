"""Dispatch intents survive concurrency, crashes and opaque native identities."""

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from backend.app.core.config import settings
from plugins.anycubic.payload import persistence


@pytest.fixture
def state_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    return persistence.state_root()


def _process_begin(base_dir, ready, results, index):
    from backend.app.core.config import settings as child_settings
    from plugins.anycubic.payload.persistence import Journal

    child_settings.base_dir = Path(base_dir)
    ready.wait(15)
    try:
        results.put(("ok", Journal("same-printer").begin("same-job", {"worker": index})))
    except Exception as error:
        results.put(("error", type(error).__name__))


def test_only_one_native_callback_thread_can_begin_a_dispatch(state_directory):
    barrier = Barrier(12)

    def begin(index):
        barrier.wait(timeout=10)
        return persistence.Journal("same-printer").begin("same-job", {"worker": index})

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(begin, range(12)))
    assert results.count(True) == 1
    assert results.count(False) == 11
    assert persistence.Journal("same-printer").get("same-job")["worker"] in range(12)


def test_only_one_application_process_can_begin_a_dispatch(state_directory):
    context = multiprocessing.get_context("spawn")
    ready, results = context.Event(), context.Queue()
    processes = [
        context.Process(target=_process_begin, args=(str(settings.base_dir), ready, results, index))
        for index in range(3)
    ]
    try:
        for process in processes:
            process.start()
        ready.set()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        outcomes = [results.get(timeout=5) for _ in processes]
        assert all(outcome[0] == "ok" for outcome in outcomes)
        assert [outcome[1] for outcome in outcomes].count(True) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("status", ["intent", "submitting", "publishing", "unknown", "accepted", "running"])
def test_reloading_an_uncertain_delivery_keeps_it_held_and_never_replays(state_directory, status):
    journal = persistence.Journal("printer")
    assert journal.begin("job", {"uri": "native.gcode"})
    journal.update("job", status=status)
    reloaded = persistence.Journal("printer")
    assert reloaded.get("job")["status"] == status
    assert [record["job_id"] for record in reloaded.unresolved()] == ["job"]
    assert not reloaded.begin("job", {"uri": "different.gcode"})


@pytest.mark.parametrize(
    "raw",
    [
        b"{not-json",
        b"[]",
        b'{"format_version":0}',
        b'{"format_version":true,"record":{}}',
        b'{"format_version":1,"record":"invalid"}',
        b'{"format_version":1,"record":{"job_id":"other","printer_key":"printer","status":"unknown"}}',
        b'{"format_version":1,"record":{"job_id":"job","printer_key":"printer","status":"unknown","weight":NaN}}',
        b'{"format_version":1,"record":{"job_id":"job","printer_key":"printer","status":"unexpected-status"}}',
        b'{"format_version":1,"record":{"job_id":"job","printer_key":"printer","status":{"unknown":true}}}',
    ],
)
def test_corrupt_intent_is_retained_and_never_becomes_permission_to_retry(state_directory, raw):
    journal = persistence.Journal("printer")
    path = journal._path("job")
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(persistence.JournalError):
        journal.get("job")
    with pytest.raises(persistence.JournalError):
        journal.unresolved()
    with pytest.raises(persistence.JournalError):
        journal.begin("job", {})
    assert path.read_bytes() == raw


def test_atomic_write_failure_preserves_previous_intent_and_cleans_temporary_file(state_directory, monkeypatch):
    journal = persistence.Journal("printer")
    assert journal.begin("job", {"status": "accepted", "receipt": "original"})
    original = journal._path("job").read_bytes()

    def fail_replace(*args):
        raise OSError("fixture disk replacement failure")

    monkeypatch.setattr(persistence.os, "replace", fail_replace)
    with pytest.raises(OSError):
        journal.update("job", status="completed")
    assert journal._path("job").read_bytes() == original
    assert journal.get("job")["status"] == "accepted"
    with pytest.raises(OSError):
        journal.begin("new-job", {})
    assert not journal._path("new-job").exists()
    assert not list(journal._path("job").parent.glob(".state-*"))


def test_printer_job_and_artifact_identities_cannot_escape_storage(state_directory):
    journal = persistence.Journal("../../opaque-printer")
    job_id, uri = "../../opaque-job/with:unicode-λ", "../../outside.gcode?token=opaque"
    assert journal.begin(job_id, {"status": "unknown"})
    journal.save_artifact(journal.printer_key, uri, {"sha256": "a" * 64})
    assert journal._path(job_id).resolve().is_relative_to(state_directory.resolve())
    assert journal.get(job_id)["job_id"] == job_id
    assert journal.artifact(journal.printer_key, uri)["sha256"] == "a" * 64
    assert not (settings.base_dir / "outside.gcode").exists()
    assert all("opaque" not in path.name for path in state_directory.rglob("*.json"))


def test_artifact_identity_mismatch_fails_closed(state_directory):
    journal = persistence.Journal("printer")
    journal.save_artifact("printer", "native.gcode", {"sha256": "a" * 64})
    path = next((state_directory / "artifacts").rglob("*.json"))
    value = json.loads(path.read_bytes())
    value["uri"] = "different.gcode"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(persistence.JournalError):
        journal.artifact("printer", "native.gcode")


def test_accounting_retry_scan_excludes_hardware_pending_and_validates_records(state_directory):
    journal = persistence.Journal("printer")
    assert journal.begin("before-cleanup", {"status": "running", "pending_outcome": "completed"})
    assert journal.begin("accounting-only", {"status": "completed", "pending_outcome": "completed"})
    assert journal.begin("finished", {"status": "completed", "pending_outcome": None})
    assert [record["job_id"] for record in journal.unresolved()] == ["before-cleanup"]
    assert [record["job_id"] for record in journal.pending_accounting()] == ["accounting-only"]
    path = journal._path("accounting-only")
    value = json.loads(path.read_bytes())
    value["record"]["pending_outcome"] = "unexpected-outcome"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(persistence.JournalError):
        journal.pending_accounting()

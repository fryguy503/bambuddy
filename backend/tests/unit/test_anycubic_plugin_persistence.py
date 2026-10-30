"""Dispatch intents survive concurrency, crashes and opaque native identities."""

import json
import multiprocessing
import types
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

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


@pytest.fixture(params=["0.1.0", "0.1.1"])
def legacy_persistence(request):
    """Run the real immutable legacy implementation in its own namespace."""
    archive = Path(__file__).resolve().parents[3] / "packages/anycubic" / f"anycubic-{request.param}.bbp"
    with zipfile.ZipFile(archive) as package:
        source = package.read("payload/persistence.py")
    module = types.ModuleType("legacy_anycubic_" + request.param.replace(".", "_"))
    exec(compile(source, str(archive) + "/payload/persistence.py", "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("accounting_pending", [False, True])
def test_legacy_observer_stale_write_after_new_completion_is_healed(
    state_directory, legacy_persistence, accounting_pending
):
    modern, legacy = persistence.Journal("printer"), legacy_persistence.Journal("printer")
    assert modern.begin("job", {"status": "running", "native_job_id": "42"})
    captured, completed = Event(), Event()

    def delayed_legacy_observer():
        stale = legacy.unresolved()[0]
        captured.set()
        assert completed.wait(timeout=5)
        # This is the active-report write made by an unmodified older version.
        return legacy.update(stale["job_id"], status="running", native_job_id="42")

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(delayed_legacy_observer)
        try:
            assert captured.wait(timeout=5)
            finalized = modern.update(
                "job", status="completed", pending_outcome="completed" if accounting_pending else None
            )
            assert finalized["finalized_outcome"] == "completed"
        finally:
            completed.set()
        assert delayed.result(timeout=5)["status"] == "running"

    # Prove the actual legacy code regressed the ordinary field and preserved
    # the unknown marker before the new reader repairs it under the OS lock.
    assert legacy.get("job")["status"] == "running"
    assert modern.unresolved() == []
    healed = legacy.get("job")
    assert healed["status"] == "completed"
    assert healed["pending_outcome"] == ("completed" if accounting_pending else None)
    assert legacy.unresolved() == []
    assert len(modern.pending_accounting()) == int(accounting_pending)

    modern.update("job", pending_outcome=None)
    # A second old observer's terminal report must not rearm accounting after
    # it has already committed successfully or been intentionally skipped.
    legacy.update("job", status="running", pending_outcome="completed")
    assert modern.pending_accounting() == []
    assert legacy.get("job")["status"] == "completed"
    assert legacy.get("job")["pending_outcome"] is None


def test_new_stale_active_report_stamps_and_preserves_legacy_completion(state_directory, legacy_persistence):
    modern, legacy = persistence.Journal("printer"), legacy_persistence.Journal("printer")
    assert legacy.begin("job", {"status": "running"})
    stale = modern.unresolved()[0]
    legacy.update("job", status="completed", pending_outcome="completed")
    result = modern.update(stale["job_id"], status="running")
    assert result["status"] == result["finalized_outcome"] == "completed"
    assert result["pending_outcome"] == "completed"
    legacy.update("job", status="accepted")
    assert modern.get("job")["status"] == "completed"
    assert legacy.unresolved() == []


def test_pending_terminal_observation_remains_replayable_until_host_cleanup(state_directory, legacy_persistence):
    modern, legacy = persistence.Journal("printer"), legacy_persistence.Journal("printer")
    assert legacy.begin("job", {"status": "running"})
    legacy.update("job", pending_outcome="completed")
    observed = modern.get("job")
    assert observed["status"] == "running" and "finalized_outcome" not in observed
    assert [record["job_id"] for record in modern.unresolved()] == ["job"]
    assert modern.pending_accounting() == []
    modern.update("job", status="completed", pending_outcome="completed")
    assert modern.get("job")["finalized_outcome"] == "completed"


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "aborted", "resolved", "rejected"])
def test_finalized_dispatch_cannot_regress_or_change_outcome(state_directory, outcome):
    journal = persistence.Journal("printer")
    assert journal.begin("job", {"status": "unknown"})
    finalized = journal.update("job", status=outcome)
    for stale in ("intent", "accepted", "running", "unknown", "failed", "completed"):
        assert journal.update("job", status=stale)["status"] == outcome
    assert journal.get("job")["finalized_outcome"] == finalized["status"]
    assert journal.unresolved() == []


def test_plate_and_label_metadata_remain_mutable_across_legacy_versions(state_directory, legacy_persistence):
    modern, legacy = persistence.Journal("printer"), legacy_persistence.Journal("printer")
    assert legacy.begin("plate:printer", {"status": "plate_required"})
    for status in ("resolved", "plate_required", "resolved"):
        legacy.update("plate:printer", status=status)
        record = modern.get("plate:printer")
        assert record["status"] == status and "finalized_outcome" not in record
    assert modern.begin("name:printer:ace:0", {"status": "metadata", "name": "Before"})
    legacy.update("name:printer:ace:0", name="After")
    assert modern.get("name:printer:ace:0")["name"] == "After"


@pytest.mark.parametrize("job_id", ["plate:printer", "name:printer:opaque-dispatch"])
def test_opaque_dispatch_ids_with_metadata_like_prefixes_still_finalize(state_directory, job_id):
    journal = persistence.Journal("printer")
    assert journal.begin(job_id, {"status": "running", "bindings": {}, "uri": "native.gcode"})
    journal.update(job_id, status="completed")
    assert journal.update(job_id, status="running")["status"] == "completed"

"""Pending dispatch visibility is minimal, explicit and never authorizes replay."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from backend.app.printers.contracts import PrinterConfig
from plugins.anycubic.payload.diagnostic import _dispatch_hold_check
from plugins.anycubic.payload.dispatch_status import summarize
from plugins.anycubic.payload.persistence import Journal
from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin


def config():
    return PrinterConfig(
        "192.168.50.50",
        "STATUS-ONLY",
        "",
        model="Kobra S1",
        plugin_id="anycubic",
        printer_id=42,
        options={"tls_fingerprint": "a" * 64},
    )


def test_public_projection_excludes_paths_secrets_and_terminal_records():
    records = [
        {
            "job_id": "queue:held",
            "status": "unknown",
            "native_filename": "C:\\private\\source-123456789abc.gcode",
            "created_at": "2026-10-31T00:00:00Z",
            "path": "/private/secret",
            "token": "PRIVATE-TOKEN",
            "bindings": {"secret": "PRIVATE"},
        },
        {"job_id": "queue:done", "status": "completed"},
    ]
    public = summarize(records)
    assert len(public) == 1 and set(public[0]) == {"job_id", "status", "native_filename", "created_at"}
    assert public[0]["native_filename"] == "source-123456789abc.gcode"
    assert "PRIVATE" not in str(public) and "private" not in str(public)


def test_cached_resolve_choices_have_no_descriptor_io_or_private_labels(monkeypatch):
    plugin = AnycubicPrinterPlugin()
    initial = plugin.create_connection(config())
    initial.journal.begin("queue:held", {"status": "unknown", "native_filename": "PRIVATE-ARCHIVE-NAME.gcode"})
    current = plugin.create_connection(config())
    monkeypatch.setattr(
        current.journal, "unresolved", lambda *_: (_ for _ in ()).throw(AssertionError("Descriptor must use cache"))
    )
    device = plugin.describe_device(config())
    field = device.operation("anycubic.job.resolve").parameters_schema["properties"]["job_id"]
    assert field["enum"] == ["queue:held"] and field["default"] == "queue:held"
    assert field["x-enum-labels"] == ["queue:held (unknown)"]
    assert "PRIVATE" not in str(field)
    current._set_dispatch_holds([])
    empty = plugin.describe_device(config()).operation("anycubic.job.resolve").parameters_schema["properties"]["job_id"]
    assert "enum" not in empty and "default" not in empty
    assert plugin.describe_device(config()).revision == device.revision


def test_read_diagnostic_exposes_count_only_and_never_clears_held_dispatch():
    plugin = AnycubicPrinterPlugin()
    connection = plugin.create_connection(config())
    connection.journal.begin("queue:private-id", {"status": "unknown", "native_filename": "PRIVATE-NAME.gcode"})
    check = _dispatch_hold_check(config(), connection)
    assert check.status == "fail" and check.params == {"count": 1}
    assert "queue:private-id" not in check.model_dump_json() and "PRIVATE" not in check.model_dump_json()
    assert connection.journal.get("queue:private-id")["status"] == "unknown"


def test_atomic_reservation_serializes_distinct_job_ids_and_keeps_legacy_holds():
    barrier = Barrier(2)

    def reserve(job_id):
        journal = Journal("ATOMIC-PRINTER")
        barrier.wait()
        return journal.reserve_dispatch(job_id, {"status": "intent", "native_filename": "same-123456789abc.gcode"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(reserve, ["queue:first", "queue:second"]))
    assert sorted(result) == [False, True]
    assert len(Journal("ATOMIC-PRINTER").unresolved()) == 1
    legacy = Journal("LEGACY-PRINTER")
    legacy.begin("queue:legacy", {"status": "unknown"})
    assert not legacy.reserve_dispatch("queue:new", {"status": "intent"})
    assert legacy.get("queue:new") is None and legacy.get("queue:legacy")["status"] == "unknown"

"""Native observations use upstream's open/closed print-run history workflow."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from backend.app.models.archive import PrintArchive
from backend.app.models.print_log import INCLUDE_OPEN_RUNS, PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.anycubic import queue_dispatch as native
from backend.tests.unit.services.test_anycubic_queue_dispatch import GCODE, case, dispatch, row, state  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("phase, outcome", [("FINISH", "completed"), ("FAILED", "failed")])
async def test_observed_native_job_opens_and_closes_one_run_with_its_queue_link(case, phase, outcome):
    case.source.write_bytes(GCODE + b"; filament used [g] = 9.87\n; total filament used [g] = 9.87\n")
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    async with case.factory() as db:
        assert await db.scalar(select(func.count()).select_from(PrintLogEntry)) == 0
        item = await db.get(PrintQueueItem, case.ids.item)
        item.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        printer = await db.get(Printer, case.ids.printer)
        printer.wear_cost_per_hour = 1.2
        await db.commit()
    await native.observe_status(case.ids.printer, state(case, record, "RUNNING"), session_factory=case.factory)
    await native.observe_status(case.ids.printer, state(case, record, "RUNNING"), session_factory=case.factory)
    async with case.factory() as db:
        # Open runs are deliberately absent from ordinary statistics/history.
        assert await db.scalar(select(func.count()).select_from(PrintLogEntry)) == 0
        entries = list((await db.scalars(select(PrintLogEntry).execution_options(**{INCLUDE_OPEN_RUNS: True}))).all())
        assert len(entries) == 1
        assert entries[0].status == "printing"
        assert entries[0].queue_item_id == case.ids.item
        assert entries[0].printer_name == "S1"
    await native.observe_status(case.ids.printer, state(case, record, phase), session_factory=case.factory)
    await native.observe_status(case.ids.printer, state(case, record, phase), session_factory=case.factory)
    async with case.factory() as db:
        entries = list((await db.scalars(select(PrintLogEntry))).all())
        assert len(entries) == 1
        entry = entries[0]
        assert entry.status == outcome
        assert entry.queue_item_id == case.ids.item
        assert entry.archive_id == record["native_archive_id"]
        assert entry.subtask_id == "native-task-7"
        assert 600 <= entry.duration_seconds < 630
        assert entry.wear_cost == pytest.approx(0.2, abs=0.001)
        assert entry.filament_used_grams == (9.87 if outcome == "completed" else None)
        assert (await db.get(PrintArchive, record["native_archive_id"])).wear_cost == entry.wear_cost


async def test_unobserved_or_rejected_submission_does_not_create_a_physical_run(case):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    await native.reconcile(case.scheduler, case.factory)
    async with case.factory() as db:
        entries = list((await db.scalars(select(PrintLogEntry).execution_options(**{INCLUDE_OPEN_RUNS: True}))).all())
        assert entries == []


async def test_native_rfid_default_compiles_as_a_postgresql_boolean():
    ddl = str(CreateTable(SpoolAssignment.__table__).compile(dialect=postgresql.dialect()))
    assert "rfid_match BOOLEAN DEFAULT false NOT NULL" in ddl

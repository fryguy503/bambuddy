"""Native lifecycle and spool consumption commit exactly once together."""

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.ams_assignment_lock import get_ams_assignment_lock
from backend.app.services.anycubic import queue_dispatch as native, usage
from backend.tests.unit.services.test_anycubic_queue_dispatch import GCODE, case, dispatch, row, state  # noqa: F401

pytestmark = pytest.mark.asyncio
SLICED = GCODE + (
    b"; filament used [g] = 9.87\n; total filament used [g] = 9.87\n; estimated printing time (normal mode) = 26m 27s\n"
)


async def assigned_case(case):
    case.source.write_bytes(SLICED)
    async with case.factory() as db:
        spool = Spool(material="PLA", label_weight=1000, weight_used=100, cost_per_kg=20)
        db.add(spool)
        await db.flush()
        db.add(SpoolAssignment(printer_id=case.ids.printer, spool_id=spool.id, ams_id=0, tray_id=2))
        item = await db.get(PrintQueueItem, case.ids.item)
        item.anycubic_options = json.dumps(
            {"use_ace": True, "filament_mapping": [{"tool": 0, "box_id": 0, "slot_id": 2}]}
        )
        await db.commit()
        return spool.id


async def consumption(case, spool_id):
    async with case.factory() as db:
        spool = await db.get(Spool, spool_id)
        rows = (await db.scalars(select(SpoolUsageHistory))).all()
        return spool.weight_used, rows


async def observe(case, record, phase):
    await native.observe_status(case.ids.printer, state(case, record, phase), session_factory=case.factory)


async def test_start_assignment_is_durable_before_publish_and_consumed_once_after_restart(case):
    spool_id = await assigned_case(case)

    def submit(*_args, **_kwargs):
        with sqlite3.connect(case.db_path) as db:
            raw = db.execute("select anycubic_dispatch from print_queue where id=?", (case.ids.item,)).fetchone()[0]
        snapshot = json.loads(raw)["usage_snapshot"]
        assert snapshot["tools"][0]["spool_id"] == spool_id
        assert snapshot["tools"][0]["grams"] == 9.87
        assert (snapshot["tools"][0]["box_id"], snapshot["tools"][0]["slot_id"]) == (0, 2)
        return {"status": "accepted", "task_id": "native-task-7"}

    case.client.submit_print.side_effect = submit
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    assert record["estimates"]["print_time_seconds"] == 1587
    async with case.factory() as db:
        archive = await db.get(PrintArchive, record["native_archive_id"])
        assert archive.print_time_seconds == 1587
        assert archive.filament_used_grams == 9.87
    assert (await consumption(case, spool_id))[0] == 100
    await observe(case, record, "RUNNING")
    state(case, record, "FINISH")
    await native.reconcile(case.scheduler, case.factory)
    await observe(case, record, "FINISH")
    await native.reconcile(case.scheduler, case.factory)
    weight, history = await consumption(case, spool_id)
    assert weight == pytest.approx(109.87)
    assert len(history) == 1
    assert history[0].archive_id == record["native_archive_id"]
    assert native.journal((await row(case)).anycubic_dispatch)["usage_result"]["state"] == "recorded"
    case.client.submit_print.assert_called_once()


@pytest.mark.parametrize("outcome", ["unknown", "failed", "cancelled", "rejected"])
async def test_uncertain_or_partial_dispatch_never_charges_full_planned_weight(case, outcome):
    spool_id = await assigned_case(case)
    case.client.submit_print.return_value = {"status": "rejected" if outcome == "rejected" else "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    if outcome in {"failed", "cancelled"}:
        await observe(case, record, "RUNNING")
        if outcome == "cancelled":
            async with case.factory() as db:
                item = await db.get(PrintQueueItem, case.ids.item)
                current = native.journal(item.anycubic_dispatch)
                current["stop_requested"] = True
                item.anycubic_dispatch = json.dumps(current)
                await db.commit()
        await observe(case, record, "FAILED")
    elif outcome == "unknown":
        # A terminal report without prior active observation is not completion.
        await observe(case, record, "FINISH")
    weight, history = await consumption(case, spool_id)
    assert weight == 100
    assert history == []


async def test_charge_and_terminal_transition_roll_back_together_on_failure(case, monkeypatch):
    spool_id = await assigned_case(case)
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await observe(case, record, "RUNNING")
    charge = usage._charge_spool

    async def fail_after_charge(db, spool, grams):
        assert await charge(db, spool, grams)
        raise RuntimeError("Injected completion failure")

    monkeypatch.setattr(usage, "_charge_spool", fail_after_charge)
    with pytest.raises(RuntimeError, match="Injected completion failure"):
        await observe(case, record, "FINISH")
    weight, history = await consumption(case, spool_id)
    assert weight == 100
    assert history == []
    item = await row(case)
    assert item.status == "printing"
    assert native.journal(item.anycubic_dispatch)["state"] == "running"
    monkeypatch.setattr(usage, "_charge_spool", charge)
    await observe(case, record, "FINISH")
    assert (await consumption(case, spool_id))[0] == pytest.approx(109.87)


async def test_stale_terminal_observer_cannot_charge_after_operator_resolution(case, monkeypatch):
    spool_id = await assigned_case(case)
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await observe(case, record, "RUNNING")
    complete = AsyncMock(wraps=usage.complete_usage)
    monkeypatch.setattr(usage, "complete_usage", complete)
    async with case.factory() as db:
        stale = await db.get(PrintQueueItem, case.ids.item)
        resolved = {**native.journal(stale.anycubic_dispatch), "state": "resolved"}
        async with case.factory() as other:
            await other.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == stale.id)
                .values(anycubic_dispatch=json.dumps(resolved))
            )
            await other.commit()
        assert not await native._observe(db, stale, state(case, record, "FINISH"))
        await db.commit()
    complete.assert_not_awaited()
    assert (await consumption(case, spool_id))[0] == 100


async def test_legacy_print_without_start_snapshot_is_not_retroactively_charged(case):
    spool_id = await assigned_case(case)
    await dispatch(case)
    async with case.factory() as db:
        item = await db.get(PrintQueueItem, case.ids.item)
        record = native.journal(item.anycubic_dispatch)
        record.pop("usage_snapshot")
        item.anycubic_dispatch = json.dumps(record)
        await db.commit()
    await observe(case, record, "RUNNING")
    await observe(case, record, "FINISH")
    assert (await row(case)).status == "completed"
    result = native.journal((await row(case)).anycubic_dispatch)["usage_result"]
    assert result["reason"] == "no_start_snapshot"
    weight, history = await consumption(case, spool_id)
    assert weight == 100
    assert history == []


async def test_completion_serializes_assignment_changes_and_notifies_only_after_commit(case, monkeypatch):
    spool_id = await assigned_case(case)
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await observe(case, record, "RUNNING")
    complete = usage.complete_usage
    notifications = []

    async def locked_completion(*args):
        assert get_ams_assignment_lock(case.ids.printer).locked()
        return await complete(*args)

    async def broadcast(message):
        if message.get("type") == "spool_usage_logged":
            assert not get_ams_assignment_lock(case.ids.printer).locked()
            assert (await consumption(case, spool_id))[0] == pytest.approx(109.87)
            notifications.append(message)

    monkeypatch.setattr(usage, "complete_usage", locked_completion)
    monkeypatch.setattr(native.ws_manager, "broadcast", broadcast)
    await observe(case, record, "FINISH")
    await native.reconcile(case.scheduler, case.factory)
    assert len(notifications) == 1
    assert notifications[0]["usage"][0]["spool_id"] == spool_id

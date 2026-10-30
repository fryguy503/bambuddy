"""Native estimates charge only captured, unchanged inventory assignments."""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.sql.dml import Update

from backend.app.models.active_print_session import ActivePrintSession  # noqa: F401 - register optional table
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import inventory_mode, spool_swaps
from backend.app.services.anycubic import usage
from backend.tests.unit.services.test_anycubic_queue_dispatch import case  # noqa: F401

pytestmark = pytest.mark.asyncio
STAMP = datetime(2026, 10, 29, 12, 0, 0)


def artifact(amounts=None, tools=(0,)):
    return SimpleNamespace(
        estimates={"anycubic_tool_usage": amounts if amounts is not None else [{"tool": 0, "grams": 9.87}]},
        filaments=tuple(SimpleNamespace(tool=tool) for tool in tools),
    )


def options(*mappings):
    return {
        "use_ace": True,
        "filament_mapping": list(mappings) or [{"tool": 0, "box_id": 1, "slot_id": 3}],
    }


@pytest.fixture
async def stock(case, monkeypatch):
    mode = AsyncMock(return_value=False)
    monkeypatch.setattr(inventory_mode, "spoolman_owns_assignments", mode)
    async with case.factory() as db:
        spools = [Spool(material="PLA", label_weight=1000, weight_used=100, cost_per_kg=30) for _ in range(2)]
        db.add_all(spools)
        await db.flush()
        assignments = [
            SpoolAssignment(printer_id=case.ids.printer, spool_id=spools[0].id, ams_id=1, tray_id=3, created_at=STAMP),
            SpoolAssignment(printer_id=case.ids.printer, spool_id=spools[1].id, ams_id=0, tray_id=0, created_at=STAMP),
        ]
        archive = PrintArchive(
            printer_id=case.ids.printer,
            filename="part.gcode",
            file_path="archive/part.gcode",
            file_size=100,
            status="completed",
            print_name="Native part",
            filament_used_grams=9.87,
        )
        db.add_all([*assignments, archive])
        await db.commit()
        ids = SimpleNamespace(
            spools=[row.id for row in spools], assignments=[row.id for row in assignments], archive=archive.id
        )
    return SimpleNamespace(case=case, mode=mode, ids=ids)


async def capture(stock, selected=None, settings=None):
    async with stock.case.factory() as db:
        printer = await db.get(Printer, stock.case.ids.printer)
        snapshot = await usage.capture_usage(db, printer, selected or artifact(), settings or options())
    return snapshot


async def complete(stock, snapshot, *, commit=True):
    async with stock.case.factory() as db:
        archive = await db.get(PrintArchive, stock.ids.archive)
        result = await usage.complete_usage(db, stock.case.ids.printer, archive, snapshot)
        if commit:
            await db.commit()
        else:
            await db.rollback()
    return result


async def weights(stock):
    async with stock.case.factory() as db:
        return [(await db.get(Spool, ident)).weight_used for ident in stock.ids.spools]


async def test_success_uses_native_physical_mapping_and_existing_atomic_inventory_writer(stock):
    snapshot = await capture(stock)
    json.dumps(snapshot)  # safe to persist in the durable queue journal
    assert snapshot["tools"][0]["assignment_id"] == stock.ids.assignments[0]
    assert snapshot["tools"][0]["assigned_at"] == STAMP.isoformat()
    result = await complete(stock, snapshot)
    assert result["state"] == "recorded"
    assert result["usage"][0]["spool_id"] == stock.ids.spools[0]
    assert result["usage"][0]["ams_id"] == 1
    assert result["usage"][0]["tray_id"] == 3
    assert await weights(stock) == pytest.approx([109.87, 100])
    async with stock.case.factory() as db:
        history = (await db.scalars(select(SpoolUsageHistory))).one()
        assert history.archive_id == stock.ids.archive
        assert history.weight_used == 9.9
        assert history.status == "completed"
        assert history.cost == 0.3
        assert (await db.get(PrintArchive, stock.ids.archive)).cost == 0.3
        assert (await db.get(Spool, stock.ids.spools[0])).last_used is not None


async def test_repeated_completion_cannot_charge_the_archive_twice(stock):
    snapshot = await capture(stock)
    await complete(stock, snapshot)
    again = await complete(stock, snapshot)
    assert again["reason"] == "usage_already_recorded"
    assert await weights(stock) == pytest.approx([109.87, 100])


async def test_charge_and_usage_history_roll_back_with_the_native_terminal_transaction(stock):
    snapshot = await capture(stock)
    assert (await complete(stock, snapshot, commit=False))["state"] == "recorded"
    assert await weights(stock) == [100, 100]
    async with stock.case.factory() as db:
        assert (await db.scalars(select(SpoolUsageHistory))).all() == []


@pytest.mark.parametrize("same_slot", [False, True])
async def test_sparse_tools_keep_each_ace_assignment_and_sum_shared_spool_usage(stock, same_slot):
    selected = artifact([{"tool": 0, "grams": 2.3}, {"tool": 2, "grams": 4.5}], tools=(0, 2))
    mapping = options(
        {"tool": 0, "box_id": 1, "slot_id": 3},
        {"tool": 2, "box_id": 1 if same_slot else 0, "slot_id": 3 if same_slot else 0},
    )
    snapshot = await capture(stock, selected, mapping)
    result = await complete(stock, snapshot)
    assert result["state"] == "recorded"
    assert await weights(stock) == pytest.approx([106.8, 100] if same_slot else [102.3, 104.5])
    async with stock.case.factory() as db:
        assert len((await db.scalars(select(SpoolUsageHistory))).all()) == (1 if same_slot else 2)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [{"tool": 0, "grams": -2}],
        [{"tool": 0, "grams": True}],
        [{"tool": 0, "grams": float("nan")}],
        [{"tool": 0, "grams": 5}, {"tool": 0, "grams": 3}],
    ],
)
async def test_missing_or_invalid_estimates_never_fabricate_a_deduction(stock, bad):
    selected = artifact()
    selected.estimates = {} if bad is None else {"anycubic_tool_usage": bad}
    snapshot = await capture(stock, selected)
    assert snapshot["state"] == "skipped"
    assert (await complete(stock, snapshot))["state"] == "skipped"
    assert await weights(stock) == [100, 100]


async def test_unknown_tool_weight_does_not_get_divided_across_known_assignments(stock):
    snapshot = await capture(stock, artifact([{"tool": 0, "grams": 9.87}], tools=(0, 2)))
    result = await complete(stock, snapshot)
    assert result["state"] == "partial"
    assert result["skipped"] == [{"tool": 2, "reason": "no_reliable_per_tool_weight"}]
    assert await weights(stock) == pytest.approx([109.87, 100])


async def test_zero_gram_tool_does_not_discard_positive_usage_for_another_tool(stock):
    selected = artifact([{"tool": 0, "grams": 0}, {"tool": 2, "grams": 4.5}], tools=(0, 2))
    mapping = options(
        {"tool": 0, "box_id": 1, "slot_id": 3},
        {"tool": 2, "box_id": 0, "slot_id": 0},
    )
    snapshot = await capture(stock, selected, mapping)
    assert [item["tool"] for item in snapshot["tools"]] == [2]
    assert (await complete(stock, snapshot))["state"] == "recorded"
    assert await weights(stock) == pytest.approx([100, 104.5])


async def test_spoolman_mode_is_explicitly_untracked_without_remote_calls(stock):
    stock.mode.return_value = True
    snapshot = await capture(stock)
    assert snapshot["mode"] == "spoolman"
    assert (await complete(stock, snapshot))["reason"] == "spoolman_usage_not_supported"
    assert await weights(stock) == [100, 100]


async def test_switching_inventory_mode_does_not_redirect_captured_usage(stock):
    snapshot = await capture(stock)
    stock.mode.return_value = True
    assert (await complete(stock, snapshot))["reason"] == "inventory_mode_changed"
    assert await weights(stock) == [100, 100]


@pytest.mark.parametrize("status", ["printing", "failed", "cancelled", "archived"])
async def test_partial_or_uncertain_prints_never_consume_the_full_slicer_estimate(stock, status):
    snapshot = await capture(stock)
    async with stock.case.factory() as db:
        (await db.get(PrintArchive, stock.ids.archive)).status = status
        await db.commit()
    assert (await complete(stock, snapshot))["reason"] == "print_not_completed"
    assert await weights(stock) == [100, 100]


async def test_old_archive_without_print_start_snapshot_is_never_retroactively_charged(stock):
    assert (await complete(stock, None))["reason"] == "no_start_snapshot"
    assert await weights(stock) == [100, 100]


async def test_removed_assignment_or_spool_is_not_replaced_with_a_current_guess(stock):
    snapshot = await capture(stock)
    async with stock.case.factory() as db:
        await db.execute(delete(SpoolAssignment).where(SpoolAssignment.id == stock.ids.assignments[0]))
        await db.execute(delete(Spool).where(Spool.id == stock.ids.spools[0]))
        await db.commit()
    result = await complete(stock, snapshot)
    assert result["state"] == "skipped"
    assert result["skipped"][0]["reason"] == "assignment_changed_during_print"
    async with stock.case.factory() as db:
        assert (await db.get(Spool, stock.ids.spools[1])).weight_used == 100


async def test_only_an_explicit_external_assignment_can_receive_external_usage(stock):
    async with stock.case.factory() as db:
        assignment = await db.get(SpoolAssignment, stock.ids.assignments[0])
        assignment.ams_id, assignment.tray_id = 255, 0
        await db.commit()
    snapshot = await capture(stock, settings={"use_ace": False, "filament_mapping": []})
    assert (await complete(stock, snapshot))["state"] == "recorded"
    assert await weights(stock) == pytest.approx([109.87, 100])


async def held_snapshot(stock):
    snapshot = await capture(stock)
    async with stock.case.factory() as db:
        item = await db.get(PrintQueueItem, stock.case.ids.item)
        item.anycubic_dispatch = json.dumps({"state": "running", usage.SNAPSHOT_KEY: snapshot})
        await db.commit()
    return snapshot


async def test_transient_a_b_a_swap_is_durable_even_when_ids_and_timestamps_are_reused(stock):
    await held_snapshot(stock)
    for spool_id in (stock.ids.spools[1], stock.ids.spools[0]):
        async with stock.case.factory() as db:
            await db.execute(delete(SpoolAssignment).where(SpoolAssignment.id == stock.ids.assignments[0]))
            db.add(
                SpoolAssignment(
                    id=stock.ids.assignments[0],
                    printer_id=stock.case.ids.printer,
                    ams_id=1,
                    tray_id=3,
                    spool_id=spool_id,
                    created_at=STAMP,
                )
            )
            await db.flush()
            await spool_swaps.record_assignment(db, stock.case.ids.printer, 1, 3, spool_id, spool_swaps.INVENTORY)
            await db.commit()
    async with stock.case.factory() as db:
        raw = (await db.get(PrintQueueItem, stock.case.ids.item)).anycubic_dispatch
    snapshot = json.loads(raw)[usage.SNAPSHOT_KEY]
    assert snapshot["invalidated_slots"] == [[1, 3]]
    result = await complete(stock, snapshot)
    assert result["state"] == "skipped"
    assert result["skipped"][0]["reason"] == "assignment_changed_during_print"
    assert await weights(stock) == [100, 100]


async def test_invalidation_rolls_back_with_a_failed_assignment(stock):
    original = await held_snapshot(stock)
    async with stock.case.factory() as db:
        await usage.invalidate_assignment(db, stock.case.ids.printer, 1, 3)
        await db.rollback()
    async with stock.case.factory() as db:
        raw = (await db.get(PrintQueueItem, stock.case.ids.item)).anycubic_dispatch
    assert json.loads(raw)[usage.SNAPSHOT_KEY] == original


async def test_invalidation_retries_status_cas_conflicts_until_the_note_is_durable(stock, monkeypatch):
    await held_snapshot(stock)
    async with stock.case.factory() as db:
        execute = db.execute
        writes = 0

        async def conflict(statement, *args, **kwargs):
            nonlocal writes
            if isinstance(statement, Update) and statement.table.name == "print_queue":
                writes += 1
                if writes < 3:
                    return SimpleNamespace(rowcount=0)
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", conflict)
        await usage.invalidate_assignment(db, stock.case.ids.printer, 1, 3)
        await db.commit()
        assert writes == 3
    async with stock.case.factory() as db:
        raw = (await db.get(PrintQueueItem, stock.case.ids.item)).anycubic_dispatch
    assert json.loads(raw)[usage.SNAPSHOT_KEY]["invalidated_slots"] == [[1, 3]]


async def test_assignment_does_not_commit_if_repeated_cas_conflicts_lose_its_ambiguity_note(stock, monkeypatch):
    await held_snapshot(stock)
    async with stock.case.factory() as db:
        execute = db.execute

        async def conflict(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "print_queue":
                return SimpleNamespace(rowcount=0)
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", conflict)
        with pytest.raises(RuntimeError, match="retry the assignment"):
            await spool_swaps.record_assignment(
                db, stock.case.ids.printer, 1, 3, stock.ids.spools[1], spool_swaps.INVENTORY
            )
        await db.rollback()


async def test_ended_journal_cannot_be_invalidated_by_a_later_assignment(stock):
    snapshot = await held_snapshot(stock)
    async with stock.case.factory() as db:
        item = await db.get(PrintQueueItem, stock.case.ids.item)
        item.anycubic_dispatch = json.dumps({"state": "completed", usage.SNAPSHOT_KEY: snapshot})
        await db.commit()
        await usage.invalidate_assignment(db, stock.case.ids.printer, 1, 3)
        await db.commit()
    async with stock.case.factory() as db:
        record = json.loads((await db.get(PrintQueueItem, stock.case.ids.item)).anycubic_dispatch)
    assert record["state"] == "completed"
    assert record[usage.SNAPSHOT_KEY] == snapshot


async def test_a_different_slot_assignment_does_not_invalidate_the_prints_spool(stock):
    snapshot = await held_snapshot(stock)
    async with stock.case.factory() as db:
        await usage.invalidate_assignment(db, stock.case.ids.printer, 0, 0)
        await db.commit()
    assert (await complete(stock, snapshot))["state"] == "recorded"

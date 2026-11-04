"""Exact native material links and transactionally idempotent real spool usage."""

import asyncio
import json
import threading
import zipfile
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import JobArtifact, JobRequest, MaterialBinding, OperationRequest
from plugins.anycubic.payload import inventory
from plugins.anycubic.payload.persistence import Journal, printer_key

SOURCE = "material:ace:10:0"


@pytest.fixture
async def inventory_config(test_engine, printer_factory, tmp_path, monkeypatch):
    printer = await printer_factory(
        plugin_id="anycubic", model="Kobra S1", plugin_options={"tls_fingerprint": "a" * 64}
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(database, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(inventory, "_async_lock", asyncio.Lock())
    return PrinterConfig.from_printer(printer)


@pytest.fixture
def make_spool(db_session):
    async def create(**changes):
        fields = {"material": "PLA", "rgba": "FF0000FF", "label_weight": 1000, "weight_used": 0, "cost_per_kg": 20}
        fields.update(changes)
        spool = Spool(**fields)
        db_session.add(spool)
        await db_session.commit()
        return spool

    return create


def boxes(material="PLA", color="FF0000FF", *, slot=0, edit=0, status=0, loaded=3):
    return [
        {
            "id": 10,
            "tray": [
                {
                    "id": slot,
                    "tray_type": material,
                    "tray_color": color,
                    "state": loaded,
                    "anycubic_edit_status": edit,
                    "anycubic_status": status,
                }
            ],
        }
    ]


def job(job_id="job-1", *, source=SOURCE, mass=None, input_id="T0"):
    return JobRequest(
        job_id=job_id,
        artifact=JobArtifact(uri="prepared.gcode", format="gcode"),
        selected_tool_ids=["tool:primary"],
        bindings=[
            MaterialBinding(input_id=input_id, tool_id="tool:primary", source_id=source, required_mass_grams=mass)
        ],
    )


async def weight(db, spool_id):
    return await db.scalar(select(Spool.weight_used).where(Spool.id == spool_id))


def remaining_boxes(percent=71, **changes):
    value = boxes(**changes)
    value[0]["tray"][0]["remain"] = percent
    return value


@pytest.mark.asyncio
async def test_selected_ace_percentage_corrects_only_balance_and_preserves_manual_lock(
    inventory_config, make_spool, db_session, monkeypatch
):
    from backend.app.core.websocket import ws_manager

    spool = await make_spool(weight_used=400, weight_used_baseline=100, weight_locked=True, last_scale_weight=850)
    native = remaining_boxes(71)
    await inventory.observe(inventory_config, native)
    broadcast = AsyncMock()
    monkeypatch.setattr(ws_manager, "broadcast", broadcast)
    result = await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert result == {
        "state": "synced",
        "source_id": SOURCE,
        "spool_id": spool.id,
        "remaining_percent": 71,
        "remaining_grams": 710,
        "weight_used": 290,
    }
    await db_session.refresh(spool)
    assert (spool.weight_used, spool.weight_used_baseline, spool.weight_locked, spool.last_scale_weight) == (
        290,
        100,
        True,
        850,
    )
    assert spool.last_weighed_at is None
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 0
    broadcast.assert_awaited_once_with({"type": "inventory_changed"})
    again = await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert again["state"] == "unchanged"
    broadcast.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("percent,expected", [(0, 500), (100, 0), (12.5, 437.5)])
async def test_ace_estimate_uses_actual_spool_capacity_and_accepts_zero(
    inventory_config, make_spool, db_session, percent, expected
):
    spool = await make_spool(label_weight=500)
    native = remaining_boxes(percent)
    await inventory.observe(inventory_config, native)
    await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert await weight(db_session, spool.id) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("percent", [None, True, -1, 101, float("nan"), float("inf"), "71"])
async def test_invalid_or_absent_ace_estimates_never_change_spool_balance(
    inventory_config, make_spool, db_session, percent
):
    spool = await make_spool(weight_used=12)
    native = remaining_boxes(percent)
    await inventory.observe(inventory_config, native)
    with pytest.raises(inventory.InventoryAssignmentError, match="valid remaining"):
        await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert await weight(db_session, spool.id) == 12


@pytest.mark.asyncio
async def test_percentage_sync_rejects_stale_or_changed_source_and_assignment(inventory_config, make_spool, db_session):
    spool = await make_spool(weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    with pytest.raises(inventory.InventoryAssignmentError, match="fresh ACE"):
        await inventory.sync_remaining(inventory_config, SOURCE, lambda: None, expected_spool_id=spool.id)
    with pytest.raises(inventory.InventoryAssignmentError, match="assigned spool changed"):
        await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id + 1)
    with pytest.raises(inventory.InventoryAssignmentError, match="ACE material changed"):
        await inventory.sync_remaining(
            inventory_config, SOURCE, lambda: remaining_boxes(color="00FF00FF"), expected_spool_id=spool.id
        )
    assert await weight(db_session, spool.id) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [0, -1])
async def test_percentage_sync_requires_positive_label_weight(inventory_config, make_spool, db_session, label):
    spool = await make_spool(label_weight=label, weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    with pytest.raises(inventory.InventoryAssignmentError, match="label weight"):
        await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert await weight(db_session, spool.id) == 12


@pytest.mark.asyncio
async def test_automatic_remaining_sync_is_per_assignment_opt_in_and_honors_weight_lock(
    inventory_config, make_spool, db_session
):
    spool = await make_spool(weight_used=12, weight_locked=True)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    assert await inventory.sync_remaining_auto(inventory_config, lambda: native) == []
    link = (await inventory.set_remaining_sync(inventory_config, SOURCE, True, spool.id))[SOURCE]
    assert link["auto_remaining_sync"] is True
    result = await inventory.sync_remaining_auto(inventory_config, lambda: native)
    assert result == [{"state": "skipped", "source_id": SOURCE, "reason": "anycubic.inventory.weight_locked"}]
    assert await weight(db_session, spool.id) == 12
    spool.weight_locked = False
    await db_session.commit()
    assert (await inventory.sync_remaining_auto(inventory_config, lambda: native))[0]["state"] == "synced"
    assert await weight(db_session, spool.id) == 290
    await inventory.set_remaining_sync(inventory_config, SOURCE, False, spool.id)
    assert await inventory.sync_remaining_auto(inventory_config, lambda: native) == []


@pytest.mark.asyncio
async def test_auto_remaining_opt_in_survives_identical_assignment_but_not_spool_or_material_changes(
    inventory_config, make_spool
):
    first, second = await make_spool(), await make_spool(rgba="00FF00FF")
    await inventory.observe(inventory_config, remaining_boxes())
    await inventory.assign(inventory_config, SOURCE, first.id)
    link = (await inventory.set_remaining_sync(inventory_config, SOURCE, True, first.id))[SOURCE]
    assert (await inventory.assign(inventory_config, SOURCE, first.id))[SOURCE] == link
    await inventory.observe(inventory_config, remaining_boxes(65))
    assert (await inventory.snapshot(inventory_config))[SOURCE] == link
    await inventory.observe(inventory_config, remaining_boxes(color="00FF00FF"))
    assert (await inventory.snapshot(inventory_config))[SOURCE].get("auto_remaining_sync") is not True
    await inventory.set_remaining_sync(inventory_config, SOURCE, True, first.id)
    assert (await inventory.assign(inventory_config, SOURCE, second.id))[SOURCE].get("auto_remaining_sync") is not True
    await inventory.set_remaining_sync(inventory_config, SOURCE, True, second.id)
    await inventory.assign(inventory_config, SOURCE, None)
    await inventory.assign(inventory_config, SOURCE, second.id)
    assert (await inventory.snapshot(inventory_config))[SOURCE].get("auto_remaining_sync") is not True


@pytest.mark.asyncio
async def test_retained_legacy_writer_cannot_carry_auto_sync_to_a_rotated_link(
    inventory_config, make_spool, db_session
):
    from plugins.anycubic.payload.persistence import storage_lock

    spool = await make_spool(weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    await inventory.set_remaining_sync(inventory_config, SOURCE, True, spool.id)
    rotations = []

    def legacy_rotate():
        with storage_lock("inventory"):
            state = inventory._load()
            link = inventory._entry(state, inventory_config)["links"][SOURCE]
            rotations.append(True)
            link["token"] = "legacy-new-material-continuity-" + str(len(rotations))
            assert link["auto_remaining_sync"] is True  # The old writer preserves unknown fields.
            inventory._save(state)

    await asyncio.to_thread(legacy_rotate)
    link = (await inventory.snapshot(inventory_config))[SOURCE]
    assert inventory.remaining_sync_enabled(link) is False
    assert await inventory.sync_remaining_auto(inventory_config, lambda: native) == []
    assert await weight(db_session, spool.id) == 12
    # An explicit new opt-in binds consent to the current continuity token.
    enabled = (await inventory.set_remaining_sync(inventory_config, SOURCE, True, spool.id))[SOURCE]
    assert inventory.remaining_sync_enabled(enabled) is True
    assert enabled["auto_remaining_token"] == enabled["token"]
    await asyncio.to_thread(legacy_rotate)
    await inventory.observe(inventory_config, native)
    healed = (await inventory.snapshot(inventory_config))[SOURCE]
    assert "auto_remaining_sync" not in healed and "auto_remaining_token" not in healed
    # The helper does not infer consent from the old boolean alone.
    assert inventory.remaining_sync_enabled({"auto_remaining_sync": True, "token": "old"}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["intent", "running", "completed"])
async def test_remaining_sync_waits_for_native_dispatch_and_deferred_accounting(
    inventory_config, make_spool, db_session, state
):
    spool = await make_spool(weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    record = {"status": state, **({"pending_outcome": "completed"} if state == "completed" else {})}
    assert Journal(printer_key(inventory_config)).begin("accounting-held-job", record)
    with pytest.raises(inventory.InventoryAssignmentError, match="filament usage has been recorded"):
        await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert await weight(db_session, spool.id) == 12


@pytest.mark.asyncio
async def test_balance_sync_after_normal_closeout_does_not_erase_usage_history_or_duplicate_deduction(
    inventory_config, make_spool, archive_factory, db_session
):
    spool = await make_spool(weight_used=200)
    native = remaining_boxes(75)
    await inventory.observe(inventory_config, native)
    request = job(mass=50)
    captured = await inventory.capture(inventory_config, request)
    await archive_factory(inventory_config.printer_id, subtask_id=request.job_id, with_run=False)
    record = {"job_id": request.job_id, "usage_snapshot": captured}
    assert (await inventory.complete(inventory_config, record, "completed"))["state"] == "recorded"
    assert await weight(db_session, spool.id) == 250
    await inventory.set_remaining_sync(inventory_config, SOURCE, True, spool.id)
    assert (await inventory.sync_remaining_auto(inventory_config, lambda: native))[0]["state"] == "unchanged"
    assert await weight(db_session, spool.id) == 250
    history = list((await db_session.scalars(select(SpoolUsageHistory))).all())
    assert len(history) == 1 and history[0].weight_used == 50
    assert (await inventory.complete(inventory_config, record, "completed"))["state"] == "already_recorded"
    assert await weight(db_session, spool.id) == 250


@pytest.mark.asyncio
async def test_changed_ace_reading_during_sync_rolls_back_balance(inventory_config, make_spool, db_session):
    spool = await make_spool(weight_used=12)
    first, second = remaining_boxes(71), remaining_boxes(70)
    await inventory.observe(inventory_config, first)
    sample = MagicMock(side_effect=[first, second])
    with pytest.raises(inventory.InventoryAssignmentError, match="ACE reading changed"):
        await inventory.sync_remaining(inventory_config, SOURCE, sample, expected_spool_id=spool.id)
    assert await weight(db_session, spool.id) == 12


@pytest.mark.asyncio
async def test_new_dispatch_reserved_during_balance_update_vetoes_commit(
    inventory_config, make_spool, db_session, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.dml import Update

    spool = await make_spool(weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    original = AsyncSession.scalar

    async def reserved_during_write(self, statement, *args, **kwargs):
        result = await original(self, statement, *args, **kwargs)
        if isinstance(statement, Update) and statement.table.name == "spool":
            assert Journal(printer_key(inventory_config)).begin("newly-reserved-job", {"status": "intent"})
        return result

    monkeypatch.setattr(AsyncSession, "scalar", reserved_during_write)
    with pytest.raises(inventory.InventoryAssignmentError, match="filament usage has been recorded"):
        await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert await weight(db_session, spool.id) == 12


@pytest.mark.asyncio
async def test_auto_sync_disabled_during_database_work_vetoes_commit(
    inventory_config, make_spool, db_session, monkeypatch
):
    from plugins.anycubic.payload import remaining

    spool = await make_spool(weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    await inventory.set_remaining_sync(inventory_config, SOURCE, True, spool.id)
    original = remaining._spool

    async def disabled_after_read(db, spool_id):
        loaded = await original(db, spool_id)
        await asyncio.to_thread(remaining._set_remaining_sync, inventory_config, SOURCE, False, spool_id)
        return loaded

    monkeypatch.setattr(remaining, "_spool", disabled_after_read)
    result = await inventory.sync_remaining_auto(inventory_config, lambda: native)
    assert result == [{"state": "skipped", "source_id": SOURCE, "reason": "anycubic.inventory.sync_disabled"}]
    assert await weight(db_session, spool.id) == 12


@pytest.mark.asyncio
async def test_final_sync_check_does_not_wait_for_a_legacy_inventory_writer_holding_sql(
    inventory_config, make_spool, db_session, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.dml import Update

    spool = await make_spool(weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    namespace = {"__name__": "legacy_balance_lock_fixture"}
    published = Path(__file__).resolve().parents[3] / "packages/anycubic/anycubic-0.1.13.bbp"
    with zipfile.ZipFile(published) as archive:
        exec(compile(archive.read("payload/persistence.py"), "published-0.1.13", "exec"), namespace)
    entered, release = threading.Event(), threading.Event()
    workers = []

    def legacy_writer():
        # Older completion code holds this file lock while awaiting its SQL
        # write. The new balance writer must not wait for it after UPDATE.
        with namespace["storage_lock"]("inventory"):
            entered.set()
            release.wait(3)

    original = AsyncSession.scalar

    async def writer_arrived_after_sql(self, statement, *args, **kwargs):
        result = await original(self, statement, *args, **kwargs)
        if isinstance(statement, Update) and statement.table.name == "spool":
            worker = threading.Thread(target=legacy_writer, daemon=True)
            workers.append(worker)
            worker.start()
            assert await asyncio.to_thread(entered.wait, 1)
        return result

    monkeypatch.setattr(AsyncSession, "scalar", writer_arrived_after_sql)
    try:
        result = await asyncio.wait_for(
            inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id), 1.5
        )
        assert result["state"] == "synced"
        assert not release.is_set()
    finally:
        release.set()
        for worker in workers:
            await asyncio.to_thread(worker.join, 2)
    assert await weight(db_session, spool.id) == 290


@pytest.mark.asyncio
async def test_sync_does_not_overwrite_a_balance_changed_after_its_read(
    inventory_config, make_spool, db_session, monkeypatch
):
    from plugins.anycubic.payload import remaining

    spool = await make_spool(weight_used=12)
    native = remaining_boxes()
    await inventory.observe(inventory_config, native)
    original = remaining._spool

    async def changed_after_read(db, spool_id):
        loaded = await original(db, spool_id)
        await db.execute(
            update(Spool)
            .where(Spool.id == spool_id)
            .values(weight_used=123)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return loaded

    monkeypatch.setattr(remaining, "_spool", changed_after_read)
    with pytest.raises(inventory.InventoryAssignmentError, match="spool balance changed"):
        await inventory.sync_remaining(inventory_config, SOURCE, lambda: native, expected_spool_id=spool.id)
    assert await weight(db_session, spool.id) == 123


@pytest.mark.asyncio
async def test_exact_rfid_link_and_token_survive_unchanged_sensor_telemetry(inventory_config, make_spool):
    spool = await make_spool()
    await inventory.observe(inventory_config, boxes())
    first = (await inventory.snapshot(inventory_config))[SOURCE]
    assert first["spool_id"] == spool.id and first["kind"] == "rfid"
    telemetry = boxes()
    telemetry[0].update(humidity=5, temp=31.5, drying_time=12)
    telemetry[0]["tray"][0].update(remain=55, nozzle_temp_min=190)
    await inventory.observe(inventory_config, telemetry)
    assert (await inventory.snapshot(inventory_config))[SOURCE]["token"] == first["token"]


@pytest.mark.asyncio
async def test_unchanged_native_reports_do_not_rewrite_inventory(inventory_config, make_spool, monkeypatch):
    await make_spool()
    save = MagicMock(wraps=inventory._save)
    monkeypatch.setattr(inventory, "_save", save)
    await inventory.observe(inventory_config, boxes())
    first = (await inventory.snapshot(inventory_config))[SOURCE]
    await inventory.observe(inventory_config, boxes())
    sensors = boxes()
    sensors[0].update(humidity=42, temp=31.5, drying_time=12)
    sensors[0]["tray"][0].update(remain=25, nozzle_temp_min=190)
    await inventory.observe(inventory_config, sensors)
    assert save.call_count == 1
    assert (await inventory.snapshot(inventory_config))[SOURCE] == first
    await inventory.observe(inventory_config, boxes(color="00FF00FF"))
    assert save.call_count == 2
    assert await inventory.snapshot(inventory_config) == {}


@pytest.mark.asyncio
async def test_repeating_the_same_manual_assignment_keeps_captured_usage_valid(
    inventory_config, make_spool, archive_factory, db_session, monkeypatch
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=20))
    original = (await inventory.snapshot(inventory_config))[SOURCE]
    save = MagicMock(wraps=inventory._save)
    monkeypatch.setattr(inventory, "_save", save)
    repeated = await inventory.assign(inventory_config, SOURCE, spool.id)
    assert repeated[SOURCE] == original
    save.assert_not_called()
    await archive_factory(inventory_config.printer_id, subtask_id="job-1", with_run=False)
    result = await inventory.complete(inventory_config, {"job_id": "job-1", "usage_snapshot": captured}, "completed")
    assert result["state"] == "recorded"
    assert await weight(db_session, spool.id) == 20


@pytest.mark.asyncio
async def test_manual_confirmation_of_an_existing_rfid_link_keeps_its_identity(inventory_config, make_spool):
    spool = await make_spool()
    await inventory.observe(inventory_config, boxes())
    original = (await inventory.snapshot(inventory_config))[SOURCE]
    assigned = (await inventory.assign(inventory_config, SOURCE, spool.id))[SOURCE]
    assert assigned["kind"] == "manual"
    assert assigned["identity"] == original["identity"]
    assert assigned["token"] == original["token"]


@pytest.mark.asyncio
async def test_slow_inventory_storage_does_not_block_the_loop_or_release_ordering_on_cancel(
    inventory_config, make_spool, monkeypatch
):
    spool = await make_spool()
    entered, release = threading.Event(), threading.Event()
    original = inventory._save
    context = ContextVar("inventory_storage_fixture", default="missing")
    context.set("request-context")

    def blocked_save(value):
        assert context.get() == "request-context"
        entered.set()
        assert release.wait(3), "Storage worker was not released by the event loop"
        original(value)

    monkeypatch.setattr(inventory, "_save", blocked_save)
    before = asyncio.all_tasks()
    first = asyncio.create_task(inventory.assign(inventory_config, SOURCE, spool.id))
    second = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        assert not first.done()
        # A callback can run while fsync/storage work remains blocked.
        pulse = asyncio.Event()
        asyncio.get_running_loop().call_soon(pulse.set)
        await asyncio.wait_for(pulse.wait(), 1)
        # Simulate shutdown's cancellation of every Task created by the
        # request. In particular, cancelling an inner to_thread Task must not
        # make the still-running filesystem worker release assignment ordering.
        for task in asyncio.all_tasks() - before:
            task.cancel()
        await asyncio.sleep(0)
        first.cancel()  # A repeated cancellation must also retain ordering.
        await asyncio.sleep(0)
        assert not first.done()
        assert inventory._async_lock.locked()
        second = asyncio.create_task(inventory.assign(inventory_config, SOURCE, spool.id))
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        try:
            await first
        except asyncio.CancelledError:
            pass
        if second is not None:
            await second
    saved = (await inventory.snapshot(inventory_config))[SOURCE]
    assert saved["spool_id"] == spool.id


@pytest.mark.asyncio
async def test_assign_operation_persists_and_publishes_without_a_native_report(inventory_config, make_spool):
    from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

    spool = await make_spool()
    events = MagicMock()
    connection = AnycubicPrinterPlugin().create_connection(inventory_config, on_event=events)
    connection.native._publish = MagicMock(side_effect=AssertionError("Inventory metadata must not send commands"))
    source = "material:ace:0:0"
    request = OperationRequest(
        operation_id="anycubic.inventory.assign", resource_id=source, parameters={"spool_id": spool.id}
    )
    receipt = await connection.invoke(request)
    assert receipt.status == "succeeded"
    assert (await inventory.snapshot(inventory_config))[source]["spool_id"] == spool.id
    events.assert_called_once()
    event = events.call_args.args[0]
    assert event.event == "device.snapshot"
    resource = next(item for item in event.data["resources"] if item["resource_id"] == source)
    assert resource["properties"]["spool_id"]["value"] == str(spool.id)
    connection.native._publish.assert_not_called()


@pytest.mark.asyncio
async def test_persisted_assignment_succeeds_even_if_status_notification_fails(inventory_config, make_spool):
    from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

    spool = await make_spool()
    events = MagicMock(side_effect=OSError("PRIVATE callback failure"))
    connection = AnycubicPrinterPlugin().create_connection(inventory_config, on_event=events)
    source = "material:ace:0:0"
    receipt = await connection.invoke(
        OperationRequest(
            operation_id="anycubic.inventory.assign", resource_id=source, parameters={"spool_id": spool.id}
        )
    )
    assert receipt.status == "succeeded" and receipt.error is None
    assert (await inventory.snapshot(inventory_config))[source]["spool_id"] == spool.id


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["missing", "archived", "host-assigned", "plugin-assigned"])
async def test_assignment_conflicts_have_specific_public_errors_and_do_not_move_spools(
    inventory_config, make_spool, db_session, unavailable
):
    from datetime import datetime

    from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

    spool = await make_spool()
    source = "material:ace:0:0"
    requested_id = spool.id
    expected = "spool_unavailable"
    if unavailable == "missing":
        requested_id += 10000
    elif unavailable == "archived":
        spool.archived_at = datetime.now()
        await db_session.commit()
    elif unavailable == "host-assigned":
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=inventory_config.printer_id, ams_id=0, tray_id=0))
        await db_session.commit()
        expected = "host_assignment_conflict"
    else:
        await inventory.assign(inventory_config, "material:ace:0:1", spool.id)
        expected = "plugin_assignment_conflict"
    before = await inventory.snapshot(inventory_config)
    connection = AnycubicPrinterPlugin().create_connection(inventory_config)
    receipt = await connection.invoke(
        OperationRequest(
            operation_id="anycubic.inventory.assign", resource_id=source, parameters={"spool_id": requested_id}
        )
    )
    assert receipt.status == "failed" and receipt.error is not None
    assert receipt.error.code == "anycubic.inventory." + expected
    assert "native operation" not in receipt.error.message
    assert await inventory.snapshot(inventory_config) == before


@pytest.mark.asyncio
async def test_assignment_storage_failure_is_specific_and_retry_preserves_existing_link(
    inventory_config, make_spool, monkeypatch
):
    from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

    first, second = await make_spool(), await make_spool(rgba="00FF00FF")
    source = "material:ace:0:0"
    await inventory.assign(inventory_config, source, first.id)
    previous = await inventory.snapshot(inventory_config)
    connection = AnycubicPrinterPlugin().create_connection(inventory_config)
    original = inventory._save
    monkeypatch.setattr(inventory, "_save", MagicMock(side_effect=OSError("PRIVATE /storage/secret permission denied")))
    request = OperationRequest(
        operation_id="anycubic.inventory.assign", resource_id=source, parameters={"spool_id": second.id}
    )
    rejected = await connection.invoke(request)
    assert rejected.status == "failed" and rejected.error is not None
    assert rejected.error.code == "anycubic.inventory.save_unconfirmed"
    assert "Refresh the assignment" in rejected.error.message
    assert "PRIVATE" not in rejected.error.message and "/storage" not in rejected.error.message
    assert await inventory.snapshot(inventory_config) == previous
    monkeypatch.setattr(inventory, "_save", original)
    assert (await connection.invoke(request)).status == "succeeded"
    assert (await inventory.snapshot(inventory_config))[source]["spool_id"] == second.id


@pytest.mark.asyncio
async def test_same_color_spools_and_same_observed_slots_are_ambiguous(inventory_config, make_spool):
    first, second = await make_spool(), await make_spool()
    await inventory.observe(inventory_config, boxes())
    assert await inventory.snapshot(inventory_config) == {}
    # Archiving the duplicate inventory record makes the physical inventory
    # unique; two loaded native slots still must not pick either arbitrarily.
    async with database.async_session() as db:
        from datetime import datetime

        duplicate = await db.get(Spool, second.id)
        duplicate.archived_at = datetime.now()
        await db.commit()
    native = boxes()
    native[0]["tray"].append(boxes(slot=1)[0]["tray"][0])
    await inventory.observe(inventory_config, native)
    assert await inventory.snapshot(inventory_config) == {}
    await inventory.observe(inventory_config, boxes())
    assert (await inventory.snapshot(inventory_config))[SOURCE]["spool_id"] == first.id


@pytest.mark.asyncio
async def test_identical_rfids_on_two_printers_revoke_automatic_link(inventory_config, make_spool):
    await make_spool()
    await inventory.observe(inventory_config, boxes())
    assert SOURCE in await inventory.snapshot(inventory_config)
    other = SimpleNamespace(serial_number="other-printer")
    await inventory.observe(other, boxes())
    assert await inventory.snapshot(inventory_config) == {}
    assert await inventory.snapshot(other) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "material,color,subtype,extra",
    [
        ("PLA", "FF0001FF", None, None),
        ("PLA", "FF0000FF", "Basic", None),
        ("PLA", "FF0000FF", "Matte", None),
        ("PETG", "FF0000FF", None, None),
        ("PLA", "FF0000FF", None, "FF0000,00FF00"),
    ],
)
async def test_material_subtype_and_color_require_exact_match_without_approximation(
    inventory_config, make_spool, material, color, subtype, extra
):
    await make_spool(subtype=subtype, extra_colors=extra)
    await inventory.observe(inventory_config, boxes(material=material, color=color))
    assert await inventory.snapshot(inventory_config) == {}


@pytest.mark.asyncio
async def test_exact_explicit_material_subtype_can_link(inventory_config, make_spool):
    spool = await make_spool(material="PLA", subtype="Basic")
    await inventory.observe(inventory_config, boxes(material="PLA Basic", color="#ff0000"))
    assert (await inventory.snapshot(inventory_config))[SOURCE]["spool_id"] == spool.id


@pytest.mark.asyncio
@pytest.mark.parametrize("edit,status,loaded", [(1, 0, 3), (0, 4, 3), (0, 0, 0), (True, 0, 3)])
async def test_manual_or_missing_rfid_evidence_never_auto_assigns(inventory_config, make_spool, edit, status, loaded):
    await make_spool()
    await inventory.observe(inventory_config, boxes(edit=edit, status=status, loaded=loaded))
    assert await inventory.snapshot(inventory_config) == {}


@pytest.mark.asyncio
async def test_host_assignment_is_not_stolen_and_invalid_resource_ids_do_not_write(
    inventory_config, make_spool, db_session
):
    spool = await make_spool()
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=inventory_config.printer_id, ams_id=0, tray_id=0))
    await db_session.commit()
    await inventory.observe(inventory_config, boxes())
    assert await inventory.snapshot(inventory_config) == {}
    with pytest.raises(ValueError):
        await inventory.assign(inventory_config, SOURCE, spool.id)
    for identity in ("../../outside", "material:ace:10:0/../../escape", "material:other:0"):
        with pytest.raises(ValueError):
            await inventory.assign(inventory_config, identity, spool.id)
    assert await inventory.snapshot(inventory_config) == {}


@pytest.mark.asyncio
async def test_new_host_assignment_revokes_previous_rfid_link(inventory_config, make_spool, db_session):
    spool = await make_spool()
    await inventory.observe(inventory_config, boxes())
    assert SOURCE in await inventory.snapshot(inventory_config)
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=inventory_config.printer_id, ams_id=0, tray_id=0))
    await db_session.commit()
    await inventory.observe(inventory_config, boxes())
    assert await inventory.snapshot(inventory_config) == {}


@pytest.mark.asyncio
async def test_artifact_per_input_estimates_and_explicit_mass_are_preserved(inventory_config, make_spool):
    first, second = await make_spool(), await make_spool(rgba="00FF00FF")
    await inventory.assign(inventory_config, SOURCE, first.id)
    await inventory.assign(inventory_config, "material:ace:10:1", second.id)
    request = job()
    request.bindings.append(
        MaterialBinding(input_id="T1", tool_id="tool:primary", source_id="material:ace:10:1", required_mass_grams=7.5)
    )
    captured = await inventory.capture(
        inventory_config,
        request,
        {
            "estimates": {
                "anycubic_tool_usage": [
                    {"tool": 0, "grams": 12.25},
                    {"tool": 1, "grams": 900},
                ]
            }
        },
    )
    assert [(item["spool_id"], item["grams"]) for item in captured["bindings"]] == [(first.id, 12.25), (second.id, 7.5)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "estimates,mass",
    [
        ({"filament_used_grams": 50}, None),
        ({"anycubic_tool_usage": [{"tool": 0, "grams": 10}, {"tool": 0, "grams": 20}]}, None),
        ({"anycubic_tool_usage": [{"tool": 0, "grams": 10}]}, 0),
        ({"anycubic_tool_usage": [{"tool": 0, "grams": float("nan")}]}, None),
    ],
)
async def test_aggregate_ambiguous_or_zero_weights_are_not_distributed_or_charged(
    inventory_config, make_spool, estimates, mass
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=mass), {"estimates": estimates})
    assert captured["bindings"] == []
    assert captured["skipped"]


@pytest.mark.asyncio
async def test_changing_assignment_a_to_b_to_a_invalidates_inflight_usage(
    inventory_config, make_spool, archive_factory, db_session
):
    first, second = await make_spool(), await make_spool(rgba="00FF00FF")
    await inventory.assign(inventory_config, SOURCE, first.id)
    captured = await inventory.capture(inventory_config, job(mass=20))
    await inventory.assign(inventory_config, SOURCE, second.id)
    await inventory.assign(inventory_config, SOURCE, first.id)
    assert (await inventory.snapshot(inventory_config))[SOURCE]["token"] != captured["bindings"][0]["token"]
    await archive_factory(inventory_config.printer_id, subtask_id="job-1", with_run=False)
    result = await inventory.complete(inventory_config, {"job_id": "job-1", "usage_snapshot": captured}, "completed")
    assert result["state"] == "skipped"
    assert await weight(db_session, first.id) == 0
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 0


@pytest.mark.asyncio
async def test_completed_usage_and_marker_commit_once_even_for_concurrent_callbacks(
    inventory_config, make_spool, archive_factory, db_session
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    request = job(mass=12.5)
    captured = await inventory.capture(inventory_config, request)
    archive = await archive_factory(inventory_config.printer_id, subtask_id=request.job_id, with_run=False)
    record = {"job_id": request.job_id, "usage_snapshot": captured}
    results = await asyncio.gather(
        *(inventory.complete(inventory_config, deepcopy(record), "completed") for _ in range(4))
    )
    assert [result["state"] for result in results].count("recorded") == 1
    assert [result["state"] for result in results].count("already_recorded") == 3
    assert await weight(db_session, spool.id) == 12.5
    history = (await db_session.scalars(select(SpoolUsageHistory))).all()
    assert len(history) == 1
    assert history[0].archive_id == archive.id and history[0].spool_id == spool.id
    assert history[0].weight_used == 12.5 and history[0].cost == 0.25
    markers = (await db_session.scalars(select(Settings).where(Settings.key.like("anycubic:usage:%")))).all()
    assert len(markers) == 1 and json.loads(markers[0].value)["state"] == "recorded"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled", "aborted"])
async def test_noncompleted_jobs_do_not_consume_filament(
    inventory_config, make_spool, archive_factory, db_session, outcome
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=12.5))
    await archive_factory(inventory_config.printer_id, subtask_id="job-1", with_run=False)
    result = await inventory.complete(inventory_config, {"job_id": "job-1", "usage_snapshot": captured}, outcome)
    assert result["state"] == "skipped"
    assert await weight(db_session, spool.id) == 0
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 0


@pytest.mark.asyncio
async def test_missing_archive_defers_without_marker_and_shared_archive_run_can_complete(
    inventory_config, make_spool, archive_factory, printer_factory, db_session
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=9))
    record = {"job_id": "job-1", "usage_snapshot": captured}
    result = await inventory.complete(inventory_config, record, "completed")
    assert result == {"state": "deferred", "reason": "archive_unavailable"}
    assert (
        await db_session.scalar(select(func.count()).select_from(Settings).where(Settings.key.like("anycubic:usage:%")))
        == 0
    )
    other = await printer_factory()
    archive = await archive_factory(other.id, subtask_id="original-shared-job", with_run=False)
    db_session.add(
        PrintLogEntry(
            printer_id=inventory_config.printer_id, subtask_id="job-1", archive_id=archive.id, status="completed"
        )
    )
    await db_session.commit()
    result = await inventory.complete(inventory_config, {**record, "archive_id": archive.id}, "completed")
    assert result["state"] == "recorded"
    assert await weight(db_session, spool.id) == 9
    assert await db_session.scalar(select(SpoolUsageHistory.archive_id)) == archive.id


@pytest.mark.asyncio
async def test_cleanup_correlates_a_shared_archive_while_its_run_is_still_open(
    inventory_config, make_spool, archive_factory, printer_factory, db_session, monkeypatch
):
    from backend.app.services.printer_manager import printer_manager
    from plugins.anycubic.payload.persistence import Journal, printer_key
    from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=14))
    original_owner = await printer_factory()
    shared = await archive_factory(original_owner.id, subtask_id="first-owner-job", with_run=False)
    run = PrintLogEntry(
        printer_id=inventory_config.printer_id, subtask_id="job-1", archive_id=shared.id, status="printing"
    )
    db_session.add(run)
    await db_session.commit()
    # This is the host's real ordering: provider cleanup runs before the open
    # log entry becomes completed, and ordinary ORM reads intentionally hide it.
    assert await db_session.scalar(select(PrintLogEntry.id).where(PrintLogEntry.id == run.id)) is None
    journal = Journal(printer_key(inventory_config))
    assert journal.begin("job-1", {"status": "running", "pending_outcome": "completed", "usage_snapshot": captured})
    plugin = AnycubicPrinterPlugin()
    plugin.connections[inventory_config.serial_number] = SimpleNamespace(config=inventory_config, journal=journal)
    monkeypatch.setattr(printer_manager, "set_awaiting_plate_clear", MagicMock())
    await plugin.cleanup_after_print(inventory_config.printer_id, {"job_id": "job-1"}, shared.id, MagicMock())
    assert journal.get("job-1")["status"] == "completed"
    assert journal.get("job-1")["pending_outcome"] is None
    assert await weight(db_session, spool.id) == 14
    assert await db_session.scalar(select(SpoolUsageHistory.archive_id)) == shared.id
    still_open = await db_session.scalar(
        select(PrintLogEntry.status).where(PrintLogEntry.id == run.id).execution_options(include_open_runs=True)
    )
    assert still_open == "printing"


@pytest.mark.asyncio
async def test_deferred_accounting_does_not_hold_hardware_and_reconnect_retries_usage_only(
    inventory_config, make_spool, archive_factory, db_session, monkeypatch
):
    from backend.app.services.printer_manager import printer_manager
    from plugins.anycubic.payload.plugin import AnycubicPrinterPlugin

    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=14))
    plugin = AnycubicPrinterPlugin()
    events = MagicMock()
    connection = plugin.create_connection(inventory_config, on_event=events)
    native_connect, native_submit = (
        MagicMock(),
        MagicMock(side_effect=AssertionError("Accounting must not replay hardware")),
    )
    monkeypatch.setattr(connection.native, "connect", native_connect)
    monkeypatch.setattr(connection.native, "submit_print", native_submit)
    monkeypatch.setattr(printer_manager, "set_awaiting_plate_clear", MagicMock())
    assert connection.journal.begin(
        "job-1", {"status": "running", "pending_outcome": "completed", "usage_snapshot": captured}
    )
    connection._plate(True)
    await plugin.cleanup_after_print(inventory_config.printer_id, {"job_id": "job-1"}, None, MagicMock())
    pending = connection.journal.get("job-1")
    assert pending["status"] == "completed" and pending["pending_outcome"] == "completed"
    assert connection.journal.unresolved() == []
    assert connection.can_deactivate()
    connection.native.state.state = "IDLE"
    monkeypatch.setattr(connection.native, "_motion_ready", lambda: True)
    assert (await connection.invoke(OperationRequest(operation_id="job.clear_plate"))).status == "succeeded"
    events.assert_called_once()
    assert events.call_args.args[0].event == "device.snapshot"
    assert events.call_args.args[0].data["extensions"]["plate_clear_required"] is False
    events.reset_mock()  # Accounting reconnects must not replay lifecycle events.
    assert connection.journal.begin("next-job", {"status": "intent"})

    scheduled = []
    monkeypatch.setattr(connection, "_schedule", scheduled.append)
    connection.connect(asyncio.get_running_loop())
    await scheduled.pop(0)
    assert connection.journal.get("job-1")["pending_outcome"] == "completed"
    assert await weight(db_session, spool.id) == 0
    await archive_factory(inventory_config.printer_id, subtask_id="job-1", with_run=False)
    connection.connect(asyncio.get_running_loop())
    await scheduled.pop(0)
    assert connection.journal.get("job-1")["pending_outcome"] is None
    assert connection.journal.get("next-job")["status"] == "intent"
    assert await weight(db_session, spool.id) == 14
    assert await db_session.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 1
    native_submit.assert_not_called()
    events.assert_not_called()
    assert native_connect.call_count == 2


@pytest.mark.asyncio
async def test_transaction_failure_rolls_back_consumption_and_marker_together(
    inventory_config, make_spool, archive_factory, db_session, monkeypatch
):
    from backend.app.services import usage_tracker

    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=8))
    await archive_factory(inventory_config.printer_id, subtask_id="job-1", with_run=False)
    original = usage_tracker._charge_spool

    async def fail_after_update(db, instance, grams):
        await original(db, instance, grams)
        raise RuntimeError("fixture crash before transaction commit")

    monkeypatch.setattr(usage_tracker, "_charge_spool", fail_after_update)
    record = {"job_id": "job-1", "usage_snapshot": captured}
    with pytest.raises(RuntimeError):
        await inventory.complete(inventory_config, record, "completed")
    assert await weight(db_session, spool.id) == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(Settings).where(Settings.key.like("anycubic:usage:%")))
        == 0
    )
    monkeypatch.setattr(usage_tracker, "_charge_spool", original)
    assert (await inventory.complete(inventory_config, record, "completed"))["state"] == "recorded"
    assert await weight(db_session, spool.id) == 8


@pytest.mark.asyncio
async def test_copied_usage_snapshot_cannot_charge_another_job(
    inventory_config, make_spool, archive_factory, db_session
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=11))
    await archive_factory(inventory_config.printer_id, subtask_id="other-job", with_run=False)
    result = await inventory.complete(
        inventory_config, {"job_id": "other-job", "usage_snapshot": captured}, "completed"
    )
    assert result["state"] == "skipped"
    assert await weight(db_session, spool.id) == 0


@pytest.mark.asyncio
async def test_unrelated_explicit_archive_is_never_used_for_consumption(
    inventory_config, make_spool, archive_factory, db_session
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=11))
    unrelated = await archive_factory(inventory_config.printer_id, subtask_id="different-job", with_run=False)
    result = await inventory.complete(
        inventory_config, {"job_id": "job-1", "archive_id": unrelated.id, "usage_snapshot": captured}, "completed"
    )
    assert result["state"] == "deferred"
    assert await weight(db_session, spool.id) == 0


@pytest.mark.asyncio
async def test_spoolman_mode_cannot_auto_assign_or_charge_builtin_spools(
    inventory_config, make_spool, archive_factory, db_session
):
    spool = await make_spool()
    await inventory.assign(inventory_config, SOURCE, spool.id)
    captured = await inventory.capture(inventory_config, job(mass=20))
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()
    await inventory.observe(inventory_config, boxes())
    with pytest.raises(ValueError):
        await inventory.assign(inventory_config, "material:ace:10:1", spool.id)
    assert (await inventory.capture(inventory_config, job(mass=20)))["reason"] == "spoolman_usage_not_supported"
    await archive_factory(inventory_config.printer_id, subtask_id="job-1", with_run=False)
    assert (await inventory.complete(inventory_config, {"job_id": "job-1", "usage_snapshot": captured}, "completed"))[
        "state"
    ] == "skipped"
    assert await weight(db_session, spool.id) == 0

"""Exact native material links and transactionally idempotent real spool usage."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
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

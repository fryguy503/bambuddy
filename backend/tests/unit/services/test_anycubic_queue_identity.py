"""LAN lifecycle observation can confirm unique jobs without a cloud task ID."""

import json

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.anycubic import queue_dispatch as native
from backend.app.services.anycubic.telemetry import Telemetry
from backend.tests.unit.services.test_anycubic_queue_dispatch import case, dispatch, row, state  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_stock_s1_info_report_recovers_the_unknown_start(case):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    telemetry = Telemetry()
    telemetry.state.connected = True
    telemetry.apply(
        "info",
        {
            "data": {
                "state": "busy",
                "project": {
                    "state": "printing",
                    "filename": record["filename"],
                    "task_id": -1,
                    "localtask": "",
                    "progress": 10,
                    "curr_layer": 5,
                    "total_layers": 125,
                },
            }
        },
    )
    await native.observe_status(case.ids.printer, telemetry.state, session_factory=case.factory)
    item = await row(case)
    assert native.journal(item.anycubic_dispatch)["state"] == "running"
    assert item.error_message is item.waiting_reason is None
    assert telemetry.state.progress == 10
    case.client.submit_print.assert_called_once()


@pytest.mark.parametrize("task_id", [None, "", "-1", "0"])
async def test_unique_lan_filename_confirms_unknown_start_without_assigned_task_id(case, task_id):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    assert record["filename"].startswith("part-")
    assert record["filename"] != "part.gcode"

    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id=task_id),
        session_factory=case.factory,
    )
    item = await row(case)
    observed = native.journal(item.anycubic_dispatch)
    assert observed["state"] == "running"
    assert observed["observed_active"] is True
    assert observed.get("task_id") not in {"-1", "0", "None"}
    assert item.waiting_reason is None
    assert item.error_message is None
    async with case.factory() as db:
        assert await native.held_printer_ids(db) == {case.ids.printer}
    await dispatch(case)
    case.client.submit_print.assert_called_once()


async def test_unique_lan_filename_observation_survives_reconcile_without_republishing(case):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    state(case, record, "RUNNING", task_id="-1")
    await native.reconcile(case.scheduler, case.factory)
    assert native.journal((await row(case)).anycubic_dispatch)["state"] == "running"
    case.client.submit_print.assert_called_once()


@pytest.mark.parametrize("task_id", ["-1", "0", None])
async def test_lan_terminal_requires_prior_matching_active_observation(case, task_id):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await native.observe_status(
        case.ids.printer,
        state(case, record, "FINISH", task_id=task_id),
        session_factory=case.factory,
    )
    assert native.journal((await row(case)).anycubic_dispatch)["state"] == "unknown"

    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id=task_id),
        session_factory=case.factory,
    )
    await native.observe_status(
        case.ids.printer,
        state(case, record, "FINISH", task_id=task_id),
        session_factory=case.factory,
    )
    item = await row(case)
    assert item.status == "completed"
    assert native.journal(item.anycubic_dispatch)["state"] == "completed"
    async with case.factory() as db:
        assert (await db.get(PrintArchive, record["native_archive_id"])).status == "completed"
        assert (await db.get(Printer, case.ids.printer)).awaiting_plate_clear is True


@pytest.mark.parametrize("filename", ["part.gcode", "other-0123456789ab.gcode", ""])
async def test_unknown_lan_start_cannot_claim_another_filename(case, filename):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id="-1", filename=filename),
        session_factory=case.factory,
    )
    assert native.journal((await row(case)).anycubic_dispatch)["state"] == "unknown"


@pytest.mark.parametrize("task_id", ["42", "-1761750000000"])
async def test_missing_lan_task_id_does_not_erase_a_known_assigned_task_id(case, task_id):
    case.client.submit_print.return_value = {"status": "accepted", "task_id": task_id}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id="-1"),
        session_factory=case.factory,
    )
    observed = native.journal((await row(case)).anycubic_dispatch)
    assert observed["state"] == "running"
    assert observed["task_id"] == task_id


@pytest.mark.parametrize("task_id", ["43", "-1761750000001"])
async def test_known_lan_task_identity_conflict_blocks_filename_fallback(case, task_id):
    case.client.submit_print.return_value = {"status": "accepted", "task_id": "-1761750000000"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id=task_id),
        session_factory=case.factory,
    )
    assert not native.journal((await row(case)).anycubic_dispatch)["observed_active"]


async def test_lan_negative_timestamp_identity_is_saved_and_enforced(case):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id="-1761750000000"),
        session_factory=case.factory,
    )
    observed = native.journal((await row(case)).anycubic_dispatch)
    assert observed["state"] == "running"
    assert observed["task_id"] == "-1761750000000"
    await native.observe_status(
        case.ids.printer,
        state(case, record, "FINISH", task_id="-1761750000001"),
        session_factory=case.factory,
    )
    assert (await row(case)).status == "printing"
    await native.observe_status(
        case.ids.printer,
        state(case, record, "FINISH", task_id="-1761750000000"),
        session_factory=case.factory,
    )
    assert (await row(case)).status == "completed"


async def test_generic_filename_in_an_uncertain_journal_cannot_use_missing_id_fallback(case):
    async with case.factory() as db:
        item = await db.get(PrintQueueItem, case.ids.item)
        item.status = "printing"
        item.anycubic_dispatch = json.dumps({"state": "unknown", "filename": "part.gcode"})
        await db.commit()
    await native.observe_status(
        case.ids.printer,
        state(case, {"filename": "part.gcode"}, "RUNNING", task_id="-1"),
        session_factory=case.factory,
    )
    assert native.journal((await row(case)).anycubic_dispatch)["state"] == "unknown"


@pytest.mark.parametrize("phase", ["RUNNING", "FINISH"])
async def test_another_live_job_cannot_keep_the_native_dispatch_confirmed(case, phase):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await native.observe_status(
        case.ids.printer, state(case, record, "RUNNING", task_id="-1"), session_factory=case.factory
    )
    await native.observe_status(
        case.ids.printer,
        state(case, record, phase, task_id="-1", filename="another-job.gcode"),
        session_factory=case.factory,
    )
    item = await row(case)
    assert item.status == "printing"
    assert native.journal(item.anycubic_dispatch)["state"] == "unknown"
    case.client.submit_print.assert_called_once()


async def test_lan_active_then_idle_still_requires_operator_outcome_review(case):
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    record = native.journal((await row(case)).anycubic_dispatch)
    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id="-1"),
        session_factory=case.factory,
    )
    await native.observe_status(
        case.ids.printer,
        state(case, record, "IDLE", task_id=None, filename=""),
        session_factory=case.factory,
    )
    item = await row(case)
    assert item.status == "printing"
    observed = native.journal(item.anycubic_dispatch)
    assert observed["state"] == "unknown"
    assert observed["observed_active"] is True
    case.client.submit_print.assert_called_once()


@pytest.mark.parametrize("task_id", [None, "-1", "0"])
async def test_manual_lan_print_without_assigned_id_still_requires_plate_clear(case, task_id):
    record = {"filename": "manual.gcode"}
    await native.observe_status(
        case.ids.printer,
        state(case, record, "RUNNING", task_id=task_id),
        session_factory=case.factory,
    )
    await native.observe_status(
        case.ids.printer,
        state(case, record, "FINISH", task_id=task_id),
        session_factory=case.factory,
    )
    async with case.factory() as db:
        assert (await db.get(Printer, case.ids.printer)).awaiting_plate_clear is True

"""Native queue cancellation never stops another task or discards uncertainty."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Select, select, update

from backend.app.api.routes import print_queue
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.anycubic import AnycubicMQTTClient
from backend.app.services.anycubic.queue_dispatch import held_printer_ids
from backend.app.services.printer_manager import printer_manager

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def native_job(async_client, db_session, monkeypatch):
    printer = Printer(
        name="S1",
        serial_number="NATIVE-STOP",
        ip_address="10.44.1.9",
        access_code="",
        model="Kobra S1",
        printer_type="anycubic",
        tls_fingerprint="a" * 64,
    )
    db_session.add(printer)
    await db_session.flush()
    item = PrintQueueItem(
        printer_id=printer.id,
        status="printing",
        anycubic_options="{}",
        anycubic_dispatch=json.dumps(
            {"state": "running", "observed_active": True, "task_id": "123", "filename": "unique-queue.gcode"}
        ),
    )
    db_session.add(item)
    await db_session.commit()
    client = AnycubicMQTTClient(
        printer.ip_address, printer.serial_number, model=printer.model, tls_fingerprint=printer.tls_fingerprint
    )
    client._client = MagicMock()
    client._subscribed = client.state.connected = True
    client.state.state = "RUNNING"
    client.state.gcode_file = client.state.subtask_name = "unique-queue.gcode"
    client.state.subtask_id = client._telemetry.task_id = "123"
    client._telemetry.phase = "printing"
    client._last_message_monotonic = client._last_state_time = time.monotonic()
    client._publish = MagicMock(return_value=True)
    monkeypatch.setattr(printer_manager, "get_client", lambda _id: client)
    return printer, item, client


async def test_matching_native_stop_keeps_the_queue_hold_until_printer_confirmation(
    async_client, db_session, native_job
):
    printer, item, client = native_job
    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")
    assert response.status_code == 200, response.text
    assert response.json()["queued"] is True
    client._publish.assert_called_once_with("print", "stop", {"taskid": "123"})
    await db_session.refresh(item)
    record = json.loads(item.anycubic_dispatch)
    assert item.status == "printing" and item.completed_at is None
    assert record["state"] == "running" and record["stop_requested"] is True
    assert printer.id in await held_printer_ids(db_session)


@pytest.mark.parametrize("phase", ["submitting", "accepted", "unknown"])
async def test_unconfirmed_native_submission_cannot_stop_a_manual_or_unrelated_print(
    async_client, db_session, native_job, phase
):
    _, item, client = native_job
    item.anycubic_dispatch = json.dumps({"state": phase, "observed_active": False, "filename": "unique-queue.gcode"})
    await db_session.commit()
    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")
    assert response.status_code == 409
    client._publish.assert_not_called()
    await db_session.refresh(item)
    assert item.status == "printing" and json.loads(item.anycubic_dispatch)["state"] == phase


@pytest.mark.parametrize("mismatch", ["filename", "task", "wire_task", "idle", "stale", "fingerprint"])
async def test_native_stop_matches_current_printer_task_and_freshness(async_client, native_job, mismatch):
    _, item, client = native_job
    if mismatch == "filename":
        client.state.gcode_file = "manual-print.gcode"
    elif mismatch == "task":
        client.state.subtask_id = "999"
    elif mismatch == "wire_task":
        client._telemetry.task_id = "999"
    elif mismatch == "idle":
        client.state.state = "IDLE"
    elif mismatch == "stale":
        client._last_state_time -= 120
    else:
        client._fingerprint = "b" * 64
    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")
    assert response.status_code == 409
    client._publish.assert_not_called()


async def test_native_batch_cancel_cannot_overwrite_a_dispatch_claim(async_client, db_session, native_job, monkeypatch):
    printer, active, _ = native_job
    batch = PrintBatch(name="Native race", quantity=3)
    db_session.add(batch)
    await db_session.flush()
    active.batch_id = batch.id
    claimed = PrintQueueItem(printer_id=printer.id, batch_id=batch.id, status="pending", anycubic_options="{}")
    waiting = PrintQueueItem(printer_id=printer.id, batch_id=batch.id, status="pending", anycubic_options="{}")
    db_session.add_all([claimed, waiting])
    await db_session.commit()
    real_execute = type(db_session).execute
    claim_injected = False

    async def execute(session, statement, *args, **kwargs):
        nonlocal claim_injected
        result = await real_execute(session, statement, *args, **kwargs)
        if (
            not claim_injected
            and isinstance(statement, Select)
            and statement.column_descriptions[0].get("expr") is PrintQueueItem
            and "pending" in statement.compile().params.values()
        ):
            # The batch has read a pending snapshot; a scheduler claims one
            # before its cancellation write. Leave the ORM snapshot stale.
            claim_injected = True
            await real_execute(
                session,
                update(PrintQueueItem)
                .where(PrintQueueItem.id == claimed.id)
                .values(status="printing", anycubic_dispatch='{"state":"submitting"}')
                .execution_options(synchronize_session=False),
            )
            await session.commit()
        return result

    monkeypatch.setattr(type(db_session), "execute", execute)
    release = AsyncMock()
    monkeypatch.setattr(print_queue, "release_budget_reservation", release)
    response = await async_client.delete(f"/api/v1/queue/batches/{batch.id}")
    assert response.status_code == 200, response.text
    assert claim_injected and "1 pending items cancelled" in response.json()["message"]
    await db_session.refresh(claimed)
    await db_session.refresh(waiting)
    await db_session.refresh(active)
    assert claimed.status == active.status == "printing"
    assert json.loads(claimed.anycubic_dispatch)["state"] == "submitting"
    assert waiting.status == "cancelled"
    assert release.await_count == 1 and release.call_args.kwargs["source_id"] == waiting.id
    states = (await real_execute(db_session, select(PrintQueueItem.status))).scalars().all()
    assert states.count("printing") == 2

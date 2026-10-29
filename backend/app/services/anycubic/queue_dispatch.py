"""Durable, non-replaying queue dispatch for the stock Anycubic LAN protocol.

The journal is committed before the print command can leave the process. A
missing receipt or a restart therefore holds the printer for reconciliation,
instead of allowing the ordinary Bambu start watchdog to print a second copy.
"""

import asyncio
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import case, select, update

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.core.websocket import ws_manager
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.archive import _copy_and_fsync
from backend.app.services.finance_budget import release_budget_reservation
from backend.app.services.print_batch import refresh_batches_after
from backend.app.services.printer_manager import printer_manager

from .client import AnycubicMQTTClient
from .files import MAX_GCODE_BYTES
from .printing import inspect_print_file
from .protocol import AnycubicError, task_id
from .transfers import transfers

logger = logging.getLogger(__name__)
HELD_STATES = frozenset({"submitting", "accepted", "unknown", "running"})
ACTIVE_STATES = frozenset({"RUNNING", "PREPARE", "PAUSE"})
CONFIRM_SECONDS = 90
_status_locks: dict[int, asyncio.Lock] = {}
_plate_active: dict[int, tuple[str, str]] = {}


def journal(value) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
        if isinstance(data, dict) and isinstance(data.get("state"), str):
            return data
    except (TypeError, ValueError):
        pass
    # An unreadable internal record cannot prove no command was sent.
    return {"state": "unknown", "error": "The native dispatch record needs operator review"}


def _lock(printer_id):
    return _status_locks.setdefault(printer_id, asyncio.Lock())


def _connection(printer):
    client = printer_manager.get_client(printer.id)
    if (
        not isinstance(client, AnycubicMQTTClient)
        or not client.state.connected
        or client.is_stale
        or client.ip_address != printer.ip_address
        or client.serial_number != printer.serial_number
        or client.model != printer.model
        or client.tls_fingerprint != printer.tls_fingerprint
    ):
        return None
    return client


async def held_printer_ids(db):
    rows = await db.execute(
        select(PrintQueueItem.printer_id, PrintQueueItem.anycubic_dispatch)
        .join(Printer, Printer.id == PrintQueueItem.printer_id)
        .where(Printer.printer_type == "anycubic", PrintQueueItem.anycubic_dispatch.is_not(None))
    )
    return {pid for pid, raw in rows.all() if journal(raw).get("state") in HELD_STATES}


async def _event(method, **fields):
    try:
        await method(**fields)
    except Exception:
        logger.debug("Native queue notification could not be delivered")


async def _known_failure(scheduler, db, item, message):
    await db.refresh(item)
    record = journal(item.anycubic_dispatch)
    if record.get("observed_active") or record.get("state") in {"completed", "resolved"}:
        return False  # Observed hardware activity wins over a contradictory receipt.
    record.update(state="failed", error=message)
    item.anycubic_dispatch = json.dumps(record)
    if record.get("native_archive_id"):
        archive = await db.get(PrintArchive, record["native_archive_id"])
        if archive is not None:
            archive.status = "failed"
            archive.completed_at = datetime.now(timezone.utc)
    await scheduler._fail_dispatch(db, item, message)
    await _event(
        ws_manager.send_queue_item_failed,
        user_id=item.created_by_id,
        queue_item_id=item.id,
        printer_id=item.printer_id,
        reason=message,
    )
    return True


async def _cancelled_before_publish(db, item, record):
    """An explicit cancellation before submit cannot leave an uncertain hold."""
    raw = (
        await db.execute(select(PrintQueueItem.anycubic_dispatch).where(PrintQueueItem.id == item.id))
    ).scalar_one_or_none()
    current = journal(raw)
    if current.get("dispatch_id") != record.get("dispatch_id") or current.get("state") not in {
        "uploading",
        "submitting",
    }:
        return
    record = {**current, "state": "cancelled"}
    await db.execute(
        update(PrintQueueItem)
        .where(PrintQueueItem.id == item.id, PrintQueueItem.anycubic_dispatch == raw)
        .values(anycubic_dispatch=json.dumps(record), waiting_reason=None)
        .execution_options(synchronize_session=False)
    )
    if record.get("native_archive_id"):
        archive = await db.get(PrintArchive, record["native_archive_id"])
        if archive is not None:
            archive.status = "cancelled"
            archive.completed_at = datetime.now(timezone.utc)
    await db.commit()


async def _source(db, item):
    if item.library_file_id is not None:
        library = (
            await db.execute(LibraryFile.active().where(LibraryFile.id == item.library_file_id))
        ).scalar_one_or_none()
        if library is None:
            raise AnycubicError("The queued library file is missing or in the trash")
        path = Path(library.file_path)
        return (path if path.is_absolute() else settings.base_dir / path), library.filename, library
    if item.archive_id is not None:
        archive = await db.get(PrintArchive, item.archive_id)
        if archive is not None:
            return settings.base_dir / archive.file_path, archive.filename, None
    raise AnycubicError("A sliced Anycubic G-code source is required")


async def _snapshot(db, item, printer, dispatch_id):
    path, filename, library = await _source(db, item)
    if not filename.lower().endswith(".gcode") or not path.is_file() or not 0 < path.stat().st_size <= MAX_GCODE_BYTES:
        raise AnycubicError("Queue a sliced Anycubic .gcode file no larger than 512 MiB")
    folder = settings.archive_dir / str(printer.id) / f"anycubic-queue-{item.id}-{dispatch_id}"
    target = folder / "print.gcode"

    def copy():
        folder.mkdir(parents=True, exist_ok=False)
        try:
            _copy_and_fsync(path, target)
        except Exception:
            target.unlink(missing_ok=True)
            folder.rmdir()
            raise

    await asyncio.to_thread(copy)
    artifact = await asyncio.to_thread(inspect_print_file, target, filename, printer.model)
    metadata = dict(library.file_metadata or {}) if library else {}
    metadata.update(printer_type="anycubic", sliced_for_model=artifact.file.model)
    archive = PrintArchive(
        printer_id=printer.id,
        filename=filename,
        file_path=str(target.relative_to(settings.base_dir)),
        file_size=artifact.file.size,
        content_hash=artifact.file.sha256,
        print_name=filename[:-6],
        sliced_for_model=artifact.file.model,
        status="archived",
        extra_data=metadata,
        created_by_id=item.created_by_id,
        project_id=item.project_id,
        library_file_id=item.library_file_id,
        print_time_seconds=item.print_time_seconds,
    )
    db.add(archive)
    await db.flush()
    return artifact, archive


async def dispatch(scheduler, db, item, printer):
    """Upload once, verify its catalog entry, commit intent, then submit once."""
    if not item.anycubic_options:
        await _known_failure(
            scheduler, db, item, "Native Anycubic queue settings are missing; queue this job again for its printer"
        )
        return
    prior = journal(item.anycubic_dispatch)
    if prior.get("state") in HELD_STATES:
        return
    if prior.get("state") == "uploading":
        await _known_failure(
            scheduler, db, item, "The previous upload was interrupted; inspect the printer files and retry manually"
        )
        return
    client = _connection(printer)
    if client is None or not client.print_capabilities()["ready"]:
        item.waiting_reason = "Waiting for fresh idle Anycubic print capabilities"
        await db.commit()
        return
    if transfers.active(printer.id):
        item.waiting_reason = "Waiting for the current printer file transfer"
        await db.commit()
        return
    cancelled = threading.Event()
    scheduler._native_cancel_events[item.id] = cancelled
    sent = False
    try:
        dispatch_id = uuid.uuid4().hex
        artifact, archive = await _snapshot(db, item, printer, dispatch_id)
        record = {
            "version": 1,
            "state": "uploading",
            "dispatch_id": dispatch_id,
            "filename": artifact.file.filename,
            "observed_active": False,
            "native_archive_id": archive.id,
            "source_library_file_id": item.library_file_id,
            "source_filename": archive.filename,
            "model": artifact.file.model,
        }
        claim = await db.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == item.id, PrintQueueItem.status == "pending")
            .values(anycubic_dispatch=json.dumps(record), archive_id=None, waiting_reason=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if claim.rowcount != 1:
            return
        if cancelled.is_set():
            await _cancelled_before_publish(db, item, record)
            return
        await db.refresh(item)
        context = await asyncio.to_thread(client.file_session, require_upload=True)
        if cancelled.is_set():
            await _cancelled_before_publish(db, item, record)
            return
        job = transfers.start(printer.id, client, artifact.file)
        await _event(
            ws_manager.send_queue_item_uploading,
            user_id=item.created_by_id,
            queue_item_id=item.id,
            printer_id=printer.id,
            printer_name=printer.name,
            file_name=archive.filename,
            total_bytes=artifact.file.size,
        )
        last_sent = -1
        while job.task is not None and not job.task.done():
            if cancelled.is_set():
                await transfers.stop(job)
                await _cancelled_before_publish(db, item, record)
                return
            if job.sent != last_sent:
                last_sent = job.sent
                await _event(
                    ws_manager.send_queue_item_upload_progress,
                    user_id=item.created_by_id,
                    queue_item_id=item.id,
                    bytes_transferred=job.sent,
                    total_bytes=job.size,
                )
            await asyncio.wait({job.task}, timeout=0.2)
        if job.task is not None:
            await job.task
        if job.phase != "completed":
            raise AnycubicError(
                job.message or "The upload could not be verified; inspect printer files before retrying"
            )
        if cancelled.is_set():
            await _cancelled_before_publish(db, item, record)
            return
        if not client.file_session_valid(context) or _connection(printer) is not client:
            raise AnycubicError("The printer connection changed after upload; retry the queued job manually")
        if not client.print_capabilities()["ready"]:
            raise AnycubicError("The printer became busy after upload; retry when it is idle")
        options = json.loads(item.anycubic_options or "{}")
        now = datetime.now(timezone.utc)
        record.update(state="submitting", submitted_at=now.isoformat())
        async with _lock(printer.id):
            claim = await db.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == item.id, PrintQueueItem.status == "pending")
                .values(
                    status="printing",
                    started_at=now,
                    anycubic_dispatch=json.dumps(record),
                    waiting_reason="Awaiting Anycubic print confirmation",
                    library_file_id=None,
                    archive_id=None,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if claim.rowcount != 1:
                return
            if cancelled.is_set():
                await _cancelled_before_publish(db, item, record)
                return
        # There is now a durable marker for every possible publish outcome.
        sent = True
        result = await asyncio.to_thread(
            client.submit_print, context, artifact, options, dispatch_id, cancelled=cancelled
        )
        async with _lock(printer.id):
            await db.refresh(item)
            record = journal(item.anycubic_dispatch)
            if record.get("dispatch_id") != dispatch_id or record.get("state") in {"completed", "failed", "resolved"}:
                return
            if result.get("status") == "rejected" and not record.get("observed_active"):
                sent = False
                await _known_failure(
                    scheduler, db, item, result.get("error") or "The printer rejected this print request"
                )
                return
            if not record.get("observed_active"):
                record["state"] = "accepted" if result.get("status") == "accepted" else "unknown"
                if result.get("task_id"):
                    record["task_id"] = result["task_id"]
                item.waiting_reason = (
                    "Awaiting Anycubic print confirmation"
                    if record["state"] == "accepted"
                    else "Print-start outcome is unknown. Check the printer before resolving this job."
                )
                item.error_message = None if record["state"] == "accepted" else item.waiting_reason
            item.anycubic_dispatch = json.dumps(record)
            await db.commit()
        scheduler._unconfirmed_budget_reservations.discard(item.id)
    except AnycubicError as exc:
        sent = False  # submit_print raises only before publishing.
        sent = not await _known_failure(scheduler, db, item, str(exc))
    except asyncio.CancelledError:
        if sent:
            # A process interruption after intent retains the hold on restart.
            scheduler._unconfirmed_budget_reservations.discard(item.id)
        raise
    except Exception:
        logger.exception("Native queue item %s could not finish dispatch", item.id)
        if sent:
            scheduler._unconfirmed_budget_reservations.discard(item.id)
        else:
            await _known_failure(scheduler, db, item, "The native dispatch failed before a print command was sent")
    finally:
        scheduler._native_cancel_events.pop(item.id, None)
        if sent:
            scheduler._unconfirmed_budget_reservations.discard(item.id)


def _matches(record, state):
    filename = PurePosixPath((state.gcode_file or state.subtask_name or "").replace("\\", "/")).name
    if not filename or filename != record.get("filename"):
        return False
    reported, assigned = task_id(state.subtask_id), task_id(record.get("task_id"))
    if reported is not None:
        return assigned is None or reported == assigned
    # Stock S1 LAN jobs report -1 instead of an assigned task ID. The exact
    # per-upload random filename still identifies this durable submission;
    # the source/display filename alone must never grant that authority.
    return bool(re.fullmatch(r".+-[0-9a-f]{12}\.gcode", filename))


def _plate_observation(printer_id, state):
    """A print can leave a part behind even when it was started at the screen."""
    name = PurePosixPath((state.gcode_file or state.subtask_name or "").replace("\\", "/")).name
    reported = task_id(state.subtask_id) or ""
    identity = (reported, name)
    if state.state in ACTIVE_STATES and name:
        _plate_active[printer_id] = identity
        return False
    previous = _plate_active.get(printer_id)
    matches = bool(previous and name == previous[1] and (not reported or not previous[0] or reported == previous[0]))
    return bool(previous and (state.state == "IDLE" or state.state in {"FINISH", "FAILED"} and matches))


async def _mark_unknown(db, item, record, message):
    record["state"] = "unknown"
    changed = await db.execute(
        update(PrintQueueItem)
        .where(PrintQueueItem.id == item.id, PrintQueueItem.anycubic_dispatch == item.anycubic_dispatch)
        .values(anycubic_dispatch=json.dumps(record), waiting_reason=message, error_message=message)
        .execution_options(synchronize_session=False)
    )
    return changed.rowcount == 1


async def _observe(db, item, state):
    raw_record = item.anycubic_dispatch
    record = journal(raw_record)
    if record.get("state") not in HELD_STATES or not state.connected:
        return False
    if _matches(record, state):
        if state.state in ACTIVE_STATES:
            first = not record.get("observed_active")
            reported = task_id(state.subtask_id)
            if (
                not first
                and record.get("state") == "running"
                and (reported is None or record.get("task_id") == reported)
            ):
                return False
            record.update(state="running", observed_active=True)
            if reported is not None:
                record["task_id"] = reported
            changed = await db.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == item.id, PrintQueueItem.anycubic_dispatch == raw_record)
                .values(anycubic_dispatch=json.dumps(record), waiting_reason=None, error_message=None)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                return False
            archive = (
                await db.get(PrintArchive, record["native_archive_id"]) if record.get("native_archive_id") else None
            )
            if archive is not None:
                archive.status = "printing"
                archive.started_at = item.started_at
                archive_task = task_id(record.get("task_id"))
                archive.subtask_id = archive_task[:64] if archive_task else None
            if first:
                await _event(
                    ws_manager.send_queue_item_acked,
                    user_id=item.created_by_id,
                    queue_item_id=item.id,
                    printer_id=item.printer_id,
                )
            return True
        if record.get("observed_active") and state.state in {"FINISH", "FAILED"}:
            outcome = (
                "cancelled" if record.get("stop_requested") else "completed" if state.state == "FINISH" else "failed"
            )
            record["state"] = outcome
            now = datetime.now(timezone.utc)
            error = (
                None
                if outcome in {"completed", "cancelled"}
                else "The Anycubic printer reported a failed or stopped print"
            )
            changed = await db.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == item.id, PrintQueueItem.anycubic_dispatch == raw_record)
                .values(
                    anycubic_dispatch=json.dumps(record),
                    completed_at=now,
                    waiting_reason=None,
                    status=case((PrintQueueItem.status == "printing", outcome), else_=PrintQueueItem.status),
                    error_message=case(
                        (PrintQueueItem.status == "printing", error), else_=PrintQueueItem.error_message
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                return False
            archive = (
                await db.get(PrintArchive, record["native_archive_id"]) if record.get("native_archive_id") else None
            )
            if archive is not None:
                archive.status, archive.completed_at = outcome, now
            if outcome == "completed" and record.get("source_library_file_id") is not None:
                library = await db.get(LibraryFile, record["source_library_file_id"])
                if library is not None:
                    library.print_count = (library.print_count or 0) + 1
                    library.last_printed_at = now
            await release_budget_reservation(db, source_type="print_queue", source_id=item.id, status="released")
            await db.execute(update(Printer).where(Printer.id == item.printer_id).values(awaiting_plate_clear=True))
            printer_manager.set_awaiting_plate_clear(item.printer_id, True)
            return True
    # IDLE or a different live job cannot prove how this observed run ended.
    if (
        record.get("observed_active")
        and state.state in ACTIVE_STATES | {"IDLE", "FINISH", "FAILED"}
        and record.get("state") != "unknown"
    ):
        return await _mark_unknown(
            db,
            item,
            record,
            "The print outcome could not be matched after reconnect. Check the printer before resolving this job.",
        )
    if not record.get("observed_active") and record.get("state") == "accepted":
        try:
            submitted = datetime.fromisoformat(record["submitted_at"])
            elapsed = (datetime.now(timezone.utc) - submitted).total_seconds()
        except (KeyError, TypeError, ValueError):
            elapsed = CONFIRM_SECONDS
        if elapsed >= CONFIRM_SECONDS:
            return await _mark_unknown(
                db,
                item,
                record,
                "The printer has not confirmed this print starting. Check the printer before resolving this job.",
            )
    return False


async def observe_status(printer_id, state, *, session_factory=None):
    """Observe native job transitions without Bambu archival or spool callbacks."""
    if not state.connected:
        return
    factory = session_factory or database.async_session
    changed = []
    async with _lock(printer_id), factory() as db:
        plate_needed = _plate_observation(printer_id, state)
        if plate_needed:
            await db.execute(update(Printer).where(Printer.id == printer_id).values(awaiting_plate_clear=True))
            printer_manager.set_awaiting_plate_clear(printer_id, True)
        rows = await db.execute(
            select(PrintQueueItem).where(
                PrintQueueItem.printer_id == printer_id, PrintQueueItem.anycubic_dispatch.is_not(None)
            )
        )
        for item in rows.scalars().all():
            if await _observe(db, item, state):
                changed.append(item.id)
        if changed or plate_needed:
            await db.commit()
            if plate_needed:
                _plate_active.pop(printer_id, None)
    if changed:
        await refresh_batches_after(changed)


async def reconcile(scheduler, session_factory):
    """Restart recovery: never turn a durable submission marker back into pending."""
    async with session_factory() as db:
        rows = await db.execute(
            select(PrintQueueItem.printer_id)
            .join(Printer, Printer.id == PrintQueueItem.printer_id)
            .where(Printer.printer_type == "anycubic", PrintQueueItem.anycubic_dispatch.is_not(None))
            .distinct()
        )
        printer_ids = list(rows.scalars().all())
    observed = []
    for printer_id in printer_ids:
        # Share the observer's lock and re-read inside it: a poll must not
        # overwrite the completion that arrived while it waited for the DB.
        async with _lock(printer_id), session_factory() as db:
            rows = await db.execute(
                select(PrintQueueItem).where(
                    PrintQueueItem.printer_id == printer_id, PrintQueueItem.anycubic_dispatch.is_not(None)
                )
            )
            for item in rows.scalars().all():
                if item.id in scheduler._inflight:
                    continue
                record = journal(item.anycubic_dispatch)
                if record.get("state") == "uploading" and item.status == "pending":
                    await _known_failure(
                        scheduler,
                        db,
                        item,
                        "The previous upload was interrupted before print submission; retry manually",
                    )
                    continue
                if record.get("state") not in HELD_STATES:
                    continue
                if record.get("state") == "submitting":
                    if not await _mark_unknown(
                        db,
                        item,
                        record,
                        "Print-start outcome is unknown after interruption. Check the printer before resolving this job.",
                    ):
                        continue
                    await db.refresh(item)
                state = printer_manager.get_status(printer_id)
                client = printer_manager.get_client(printer_id)
                if state is not None and isinstance(client, AnycubicMQTTClient) and not client.is_stale:
                    if await _observe(db, item, state):
                        observed.append(item.id)
            await db.commit()
    if observed:
        await refresh_batches_after(observed)

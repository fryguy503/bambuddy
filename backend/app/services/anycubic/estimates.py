"""Bounded backfill of sliced estimates for native jobs queued before support."""

import asyncio
import json
from pathlib import Path

from sqlalchemy import select, update

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer

from .printing import ESTIMATES_VERSION, inspect_print_file, merge_estimates
from .protocol import AnycubicError


def _current(metadata, source_hash):
    return (
        isinstance(metadata, dict)
        and metadata.get("anycubic_estimates_version") == ESTIMATES_VERSION
        and metadata.get("anycubic_estimates_sha256") == source_hash
    )


async def _read(path, filename, model, source_hash):
    try:
        artifact = await asyncio.to_thread(inspect_print_file, path, filename, model)
        if source_hash and artifact.file.sha256 != source_hash:
            raise AnycubicError("The stored file hash no longer matches its content")
        return artifact.estimates
    except (AnycubicError, OSError):
        # Remember the failed attempt too. A changed source hash or a future
        # parser version permits another attempt without reopening it each tick.
        return {
            "anycubic_estimates_version": ESTIMATES_VERSION,
            "anycubic_estimates_sha256": source_hash,
            "anycubic_estimates_error": True,
        }


async def hydrate_queue_estimates(session_factory, *, limit=2):
    """Update existing queue displays without changing dispatch or usage state."""
    remaining = limit
    async with session_factory() as db:
        rows = await db.execute(
            select(LibraryFile, Printer.model)
            .join(PrintQueueItem, PrintQueueItem.library_file_id == LibraryFile.id)
            .join(Printer, PrintQueueItem.printer_id == Printer.id)
            .where(
                Printer.printer_type == "anycubic",
                PrintQueueItem.status == "pending",
                LibraryFile.deleted_at.is_(None),
            )
            .order_by(LibraryFile.id)
        )
        seen = set()
        for library, model in rows.all():
            if remaining <= 0:
                break
            if library.id in seen or not library.filename.lower().endswith(".gcode"):
                continue
            seen.add(library.id)
            if _current(library.file_metadata, library.file_hash):
                continue
            source_hash = library.file_hash
            stored = Path(library.file_path)
            path = stored if stored.is_absolute() else settings.base_dir / stored
            estimates = await _read(path, library.filename, model, source_hash)
            remaining -= 1
            await db.refresh(library)
            if library.file_hash != source_hash or library.deleted_at is not None:
                continue
            library.file_metadata = merge_estimates(library.file_metadata, estimates)
            if not source_hash and estimates.get("anycubic_estimates_sha256"):
                library.file_hash = estimates["anycubic_estimates_sha256"]
            await db.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.library_file_id == library.id, PrintQueueItem.status == "pending")
                .values(print_time_seconds=estimates.get("print_time_seconds"))
            )

        # Native submission detaches both source FKs to retain its no-replay
        # record. Its immutable history copy can restore display estimates,
        # but must never reconstruct a missing print-start spool snapshot.
        rows = await db.execute(
            select(PrintQueueItem)
            .join(Printer, PrintQueueItem.printer_id == Printer.id)
            .where(Printer.printer_type == "anycubic", PrintQueueItem.anycubic_dispatch.is_not(None))
            .order_by(PrintQueueItem.id)
        )
        for item in rows.scalars().all():
            if remaining <= 0:
                break
            try:
                record = json.loads(item.anycubic_dispatch or "{}")
                archive_id = record.get("native_archive_id")
                if type(archive_id) is not int:
                    continue
            except (TypeError, ValueError, AttributeError):
                continue
            archive = await db.get(PrintArchive, archive_id)
            if archive is None or archive.deleted_at is not None:
                continue
            if _current(record.get("estimates"), archive.content_hash):
                continue
            source_hash, source_path = archive.content_hash, archive.file_path
            estimates = await _read(settings.base_dir / source_path, archive.filename, record.get("model"), source_hash)
            remaining -= 1
            await db.refresh(archive)
            if (
                archive.deleted_at is not None
                or archive.content_hash != source_hash
                or archive.file_path != source_path
            ):
                continue
            record["estimates"] = estimates
            changed = await db.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == item.id, PrintQueueItem.anycubic_dispatch == item.anycubic_dispatch)
                .values(anycubic_dispatch=json.dumps(record), print_time_seconds=estimates.get("print_time_seconds"))
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount == 1:
                if not source_hash and estimates.get("anycubic_estimates_sha256"):
                    archive.content_hash = estimates["anycubic_estimates_sha256"]
                archive.print_time_seconds = estimates.get("print_time_seconds")
                archive.filament_used_grams = estimates.get("filament_used_grams")
                archive.extra_data = merge_estimates(archive.extra_data, estimates)
        await db.commit()

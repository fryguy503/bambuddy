"""Old native queue entries gain display estimates without changing print intent."""

import hashlib
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.api.routes.print_queue import _enrich_response
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.anycubic import estimates, queue_dispatch as native
from backend.tests.unit.services.test_anycubic_queue_dispatch import GCODE, case, dispatch, row  # noqa: F401

pytestmark = pytest.mark.asyncio
SLICED = GCODE + (
    b"; filament used [g] = 9.87\n; total filament used [g] = 9.87\n; estimated printing time (normal mode) = 26m 27s\n"
)


async def test_legacy_archive_without_hash_is_backfilled_only_once(case, monkeypatch):
    case.source.write_bytes(SLICED)
    await dispatch(case)
    async with case.factory() as db:
        item = await db.get(PrintQueueItem, case.ids.item)
        record = native.journal(item.anycubic_dispatch)
        record.pop("estimates")
        record.pop("usage_snapshot")
        item.anycubic_dispatch = json.dumps(record)
        archive = await db.get(PrintArchive, record["native_archive_id"])
        archive.content_hash = None
        archive.print_time_seconds = None
        archive.filament_used_grams = None
        await db.commit()
    inspect = MagicMock(wraps=estimates.inspect_print_file)
    monkeypatch.setattr(estimates, "inspect_print_file", inspect)
    await estimates.hydrate_queue_estimates(case.factory, limit=1)
    await estimates.hydrate_queue_estimates(case.factory, limit=1)
    assert inspect.call_count == 1
    async with case.factory() as db:
        archive = await db.get(PrintArchive, record["native_archive_id"])
        assert archive.content_hash == hashlib.sha256(SLICED).hexdigest()
        assert archive.filament_used_grams == 9.87
    assert "usage_snapshot" not in native.journal((await row(case)).anycubic_dispatch)


async def test_backfill_is_bounded_per_distinct_source_and_fills_all_queued_copies(case, monkeypatch):
    case.source.write_bytes(SLICED)
    second_path = case.source.with_name("second.gcode")
    second_path.write_bytes(SLICED)
    async with case.factory() as db:
        first = await db.get(LibraryFile, case.ids.library)
        first.file_metadata = {**first.file_metadata, "user_note": "keep"}
        second = LibraryFile(
            filename="second.gcode", file_path=str(second_path), file_type="gcode", file_size=len(SLICED)
        )
        db.add(second)
        await db.flush()
        second_id = second.id
        for source_id in (first.id, first.id, second.id):
            db.add(PrintQueueItem(printer_id=case.ids.printer, library_file_id=source_id, position=2))
        await db.commit()
    inspect = MagicMock(wraps=estimates.inspect_print_file)
    monkeypatch.setattr(estimates, "inspect_print_file", inspect)
    await estimates.hydrate_queue_estimates(case.factory, limit=1)
    assert inspect.call_count == 1
    async with case.factory() as db:
        first = await db.get(LibraryFile, case.ids.library)
        second = await db.get(LibraryFile, second_id)
        assert first.file_metadata["filament_used_grams"] == 9.87
        assert first.file_metadata["user_note"] == "keep"
        assert first.file_hash == hashlib.sha256(SLICED).hexdigest()
        assert not second.file_metadata
        copies = (await db.scalars(select(PrintQueueItem).where(PrintQueueItem.library_file_id == first.id))).all()
        assert len(copies) == 3
        assert all(item.print_time_seconds == 1587 and item.status == "pending" for item in copies)
    await estimates.hydrate_queue_estimates(case.factory, limit=1)
    assert inspect.call_count == 2
    await estimates.hydrate_queue_estimates(case.factory, limit=1)
    assert inspect.call_count == 2
    case.client.submit_print.assert_not_called()


@pytest.mark.parametrize("content", [GCODE, b"not sliced G-code\x00"])
async def test_unknown_or_malformed_estimates_are_not_reparsed_every_tick(case, monkeypatch, content):
    case.source.write_bytes(content)
    inspect = MagicMock(wraps=estimates.inspect_print_file)
    monkeypatch.setattr(estimates, "inspect_print_file", inspect)
    await estimates.hydrate_queue_estimates(case.factory)
    await estimates.hydrate_queue_estimates(case.factory)
    assert inspect.call_count == 1
    async with case.factory() as db:
        source = await db.get(LibraryFile, case.ids.library)
        assert source.file_metadata["anycubic_estimates_version"] == estimates.ESTIMATES_VERSION
        assert source.file_metadata.get("filament_used_grams") is None
        # A changed indexed source is eligible again and can now gain estimates.
        case.source.write_bytes(SLICED)
        source.file_hash = hashlib.sha256(SLICED).hexdigest()
        await db.commit()
    await estimates.hydrate_queue_estimates(case.factory)
    assert inspect.call_count == 2
    async with case.factory() as db:
        source = await db.get(LibraryFile, case.ids.library)
        assert source.file_metadata["filament_used_grams"] == 9.87
        assert "anycubic_estimates_error" not in source.file_metadata


async def test_detached_history_backfill_preserves_unknown_intent_and_never_creates_usage_capture(case):
    case.source.write_bytes(SLICED)
    case.client.submit_print.return_value = {"status": "unknown"}
    await dispatch(case)
    async with case.factory() as db:
        item = await db.get(PrintQueueItem, case.ids.item)
        record = native.journal(item.anycubic_dispatch)
        record.pop("estimates")
        record.pop("usage_snapshot", None)
        item.anycubic_dispatch = json.dumps(record)
        item.print_time_seconds = None
        archive = await db.get(PrintArchive, record["native_archive_id"])
        archive.print_time_seconds = archive.filament_used_grams = None
        archive.extra_data = {"keep": "history"}
        await db.commit()
    await estimates.hydrate_queue_estimates(case.factory)
    item = await row(case)
    record = native.journal(item.anycubic_dispatch)
    assert item.library_file_id is item.archive_id is None
    assert record["state"] == "unknown"
    assert record["observed_active"] is False
    assert "usage_snapshot" not in record
    assert record["estimates"]["filament_used_grams"] == 9.87
    async with case.factory() as db:
        archive = await db.get(PrintArchive, record["native_archive_id"])
        assert archive.filament_used_grams == 9.87
        assert archive.print_time_seconds == 1587
        assert archive.extra_data["keep"] == "history"
        assert await db.scalar(select(func.count()).select_from(SpoolUsageHistory)) == 0
        linked = await db.scalar(
            select(PrintQueueItem)
            .where(PrintQueueItem.id == item.id)
            .options(
                selectinload(PrintQueueItem.printer),
                selectinload(PrintQueueItem.archive),
                selectinload(PrintQueueItem.library_file),
                selectinload(PrintQueueItem.created_by),
                selectinload(PrintQueueItem.batch),
            )
        )
        response = _enrich_response(linked)
        assert response.filament_used_grams == 9.87
        assert response.print_time_seconds == 1587
    case.client.submit_print.assert_called_once()

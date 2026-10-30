"""Native estimates feed the existing library and queue metadata workflow."""

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.tests.integration.test_anycubic_queue_api import native_source  # noqa: F401
from backend.tests.unit.services.test_anycubic_estimates import FOOTER, GCODE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_native_gcode_upload_persists_slicer_estimates(async_client, db_session):
    response = await async_client.post(
        "/api/v1/library/files", files={"file": ("S1-export.gcode", GCODE + FOOTER, "text/plain")}
    )
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["printer_type"] == "anycubic"
    assert metadata["sliced_for_model"] == "Kobra S1"
    assert metadata["print_time_seconds"] == 1587
    assert metadata["filament_used_grams"] == 9.87
    assert metadata["anycubic_tool_usage"] == [{"tool": 0, "grams": 9.87}]
    row = await db_session.get(LibraryFile, response.json()["id"])
    assert row.file_metadata == metadata


async def test_enqueue_hydrates_old_source_metadata_and_caches_sliced_duration(async_client, native_source, db_session):
    printer, source, path = native_source
    path.write_bytes(GCODE + FOOTER)
    source.file_metadata = {
        **source.file_metadata,
        "user_note": "keep",
        "print_time_seconds": 9,
        "filament_used_grams": 99,
    }
    await db_session.commit()
    response = await async_client.post(
        "/api/v1/queue/",
        json={
            "printer_id": printer.id,
            "library_file_id": source.id,
            "manual_start": True,
            "quantity": 2,
            "anycubic_options": {"use_ace": False},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["print_time_seconds"] == 1587
    assert response.json()["filament_used_grams"] == 9.87
    db_session.expire_all()
    rows = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert len(rows) == 2
    assert all(row.print_time_seconds == 1587 for row in rows)
    library = await db_session.get(LibraryFile, rows[0].library_file_id)
    assert library.file_metadata["user_note"] == "keep"
    assert library.file_metadata["anycubic_tool_usage"] == [{"tool": 0, "grams": 9.87}]


async def test_enqueue_missing_optional_stats_removes_untrusted_stale_values(async_client, native_source, db_session):
    printer, source, path = native_source
    path.write_bytes(GCODE)
    source.file_metadata = {
        **source.file_metadata,
        "print_time_seconds": 999,
        "filament_used_grams": 88,
        "anycubic_tool_usage": [{"tool": 0, "grams": 88}],
    }
    await db_session.commit()
    response = await async_client.post(
        "/api/v1/queue/",
        json={
            "printer_id": printer.id,
            "library_file_id": source.id,
            "manual_start": True,
            "anycubic_options": {"use_ace": False},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["print_time_seconds"] is None
    assert response.json()["filament_used_grams"] is None


async def test_invalid_native_mapping_does_not_commit_metadata_hydration(async_client, native_source, db_session):
    printer, source, path = native_source
    path.write_bytes(GCODE + FOOTER)
    source_id = source.id
    response = await async_client.post(
        "/api/v1/queue/",
        json={
            "printer_id": printer.id,
            "library_file_id": source_id,
            "manual_start": True,
            "anycubic_options": {"use_ace": True, "filament_mapping": []},
        },
    )
    assert response.status_code == 400
    db_session.expire_all()
    source = await db_session.get(LibraryFile, source_id)
    assert "anycubic_estimates_version" not in source.file_metadata

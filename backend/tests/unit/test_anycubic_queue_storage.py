"""Native settings survive upgrades and batch copies; dispatch intent never replays."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import backend.app.models  # noqa: F401 - register all tables
from backend.app.core.database import Base, run_migrations
from backend.app.models import (  # noqa: F401 - match init_db's optional model registration
    active_print_session,
    active_print_spoolman,
    bug_report,
    external_link,
    filament_sku_settings,
    finance,
    print_log,
    project_bom,
    shopping_list,
    slot_preset,
    spoolman_k_profile,
    spoolman_slot_assignment,
    virtual_printer,
)
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.print_batch import _clone_queue_item


def test_native_batch_copy_keeps_choices_but_gets_a_fresh_dispatch():
    source = PrintQueueItem(
        printer_id=3,
        library_file_id=None,
        archive_id=None,
        status="completed",
        anycubic_options=json.dumps({"use_ace": False}),
        anycubic_dispatch=json.dumps(
            {"state": "completed", "source_library_file_id": 17, "native_archive_id": 22, "filename": "unique.gcode"}
        ),
    )
    source.variants = []
    clone = _clone_queue_item(source, position=4, created_by_id=None)
    assert clone.library_file_id == 17 and clone.archive_id is None
    assert clone.anycubic_options == source.anycubic_options
    assert clone.anycubic_dispatch is None and clone.status == "pending"


@pytest.mark.asyncio
async def test_existing_database_gets_nullable_native_columns_idempotently(tmp_path, monkeypatch):
    from backend.app.core import database, db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(database, "is_sqlite", lambda: True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native-queue.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Simulate the deployed pre-native table without touching its rows.
            await conn.execute(text("ALTER TABLE print_queue DROP COLUMN anycubic_options"))
            await conn.execute(text("ALTER TABLE print_queue DROP COLUMN anycubic_dispatch"))
            await run_migrations(conn)
            await conn.execute(
                PrintQueueItem.__table__.insert().values(
                    id=9001,
                    anycubic_options="{}",
                    anycubic_dispatch=json.dumps({"state": "unknown"}),
                )
            )
            await run_migrations(conn)
            columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(print_queue)"))).all()}
            journal = await conn.scalar(text("SELECT anycubic_dispatch FROM print_queue WHERE id=9001"))
            assert {"anycubic_options", "anycubic_dispatch"} <= columns
            assert json.loads(journal)["state"] == "unknown"
    finally:
        await engine.dispose()

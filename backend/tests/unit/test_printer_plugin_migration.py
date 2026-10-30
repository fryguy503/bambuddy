"""Existing printer records acquire bundled support without losing settings."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database, db_dialect
from backend.app.models.printer import Printer


@pytest.mark.asyncio
async def test_printer_plugin_migration_preserves_legacy_and_installed_records(tmp_path, monkeypatch):
    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(database, "is_sqlite", lambda: True)
    database.load_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'printer-plugin-migration.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            legacy = Printer(
                name="Existing Bambu printer",
                serial_number="LEGACY-SERIAL",
                ip_address="192.0.2.20",
                access_code="legacy-secret",
                model="P1S",
                location="Workshop",
                is_active=False,
                auto_archive=False,
            )
            session.add(legacy)
            await session.commit()
            printer_id = legacy.id

        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE printers DROP COLUMN plugin_id"))
            await conn.execute(text("ALTER TABLE printers DROP COLUMN plugin_options"))
            await database.run_migrations(conn)
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT plugin_id, plugin_options, name, serial_number, access_code, location, is_active, auto_archive "
                            "FROM printers WHERE id=:id"
                        ),
                        {"id": printer_id},
                    )
                )
                .mappings()
                .one()
            )
            assert dict(row) == {
                "plugin_id": "bambu",
                "plugin_options": None,
                "name": "Existing Bambu printer",
                "serial_number": "LEGACY-SERIAL",
                "access_code": "legacy-secret",
                "location": "Workshop",
                "is_active": 0,
                "auto_archive": 0,
            }
            await conn.execute(
                text("UPDATE printers SET plugin_id='test_printer', plugin_options=:options WHERE id=:id"),
                {"id": printer_id, "options": json.dumps({"setting": "preserved"})},
            )
            await database.run_migrations(conn)
            persisted = (
                await conn.execute(
                    text("SELECT plugin_id, plugin_options FROM printers WHERE id=:id"), {"id": printer_id}
                )
            ).one()
            assert persisted.plugin_id == "test_printer"
            assert json.loads(persisted.plugin_options) == {"setting": "preserved"}
    finally:
        await engine.dispose()

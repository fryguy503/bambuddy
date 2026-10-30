"""Provider migration preserves unsupported legacy identities without drivers."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core import database


@pytest.fixture
async def migration_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "is_sqlite", lambda: True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-provider.db'}")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_first_upgrade_preserves_nonempty_legacy_provider_ids(migration_engine):
    values = ["legacy_vendor", "bambu", " experimental_vendor ", "", "   ", None]
    async with migration_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, printer_type VARCHAR(64), name TEXT)"))
        await conn.execute(
            text("INSERT INTO printers (id, printer_type, name) VALUES (:id, :provider, :name)"),
            [{"id": index, "provider": provider, "name": f"Printer {index}"} for index, provider in enumerate(values)],
        )
        await database._migrate_printer_plugin_identity(conn)
        rows = (await conn.execute(text("SELECT plugin_id, printer_type, name FROM printers ORDER BY id"))).all()
        assert [row.plugin_id for row in rows] == [
            "legacy_vendor",
            "bambu",
            "experimental_vendor",
            "bambu",
            "bambu",
            "bambu",
        ]
        assert [row.printer_type for row in rows] == values
        assert [row.name for row in rows] == [f"Printer {index}" for index in range(len(values))]

        # After admission, only the new identity is authoritative. In particular
        # choosing the included provider must not revive an old unsupported ID.
        await conn.execute(text("UPDATE printers SET plugin_id='chosen_provider' WHERE id=0"))
        await conn.execute(text("UPDATE printers SET plugin_id='bambu' WHERE id=2"))
        await database._migrate_printer_plugin_identity(conn)
        repeated = (await conn.execute(text("SELECT plugin_id FROM printers ORDER BY id"))).scalars().all()
        assert repeated == ["chosen_provider", "bambu", "bambu", "bambu", "bambu", "bambu"]


@pytest.mark.asyncio
async def test_database_without_legacy_provider_metadata_keeps_included_default(migration_engine):
    async with migration_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (1, 'Existing printer')"))
        await database._migrate_printer_plugin_identity(conn)
        assert await conn.scalar(text("SELECT plugin_id FROM printers WHERE id=1")) == "bambu"


@pytest.mark.asyncio
async def test_existing_plugin_ids_are_never_backfilled_from_stale_legacy_values(migration_engine):
    async with migration_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE printers (id INTEGER PRIMARY KEY, printer_type VARCHAR(64), "
                "plugin_id VARCHAR(64) NOT NULL DEFAULT 'bambu')"
            )
        )
        await conn.execute(
            text("INSERT INTO printers (id, printer_type, plugin_id) VALUES (:id, :legacy, :current)"),
            [
                {"id": 1, "legacy": "old_vendor", "current": "chosen_provider"},
                {"id": 2, "legacy": "old_vendor", "current": "bambu"},
                {"id": 3, "legacy": "bambu", "current": "unsupported_provider"},
            ],
        )
        await database._migrate_printer_plugin_identity(conn)
        await database._migrate_printer_plugin_identity(conn)
        rows = (await conn.execute(text("SELECT plugin_id FROM printers ORDER BY id"))).scalars().all()
        assert rows == ["chosen_provider", "bambu", "unsupported_provider"]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_plugin_column,legacy_column", [(False, True), (False, False), (True, True)])
async def test_postgres_column_detection_and_one_time_backfill(monkeypatch, existing_plugin_column, legacy_column):
    monkeypatch.setattr(database, "is_sqlite", lambda: False)
    columns = {"id"}
    if existing_plugin_column:
        columns.add("plugin_id")
    if legacy_column:
        columns.add("printer_type")
    queries = []
    updates = []

    async def execute(statement, parameters=None):
        sql = str(statement)
        if "information_schema.columns" in sql:
            assert parameters["table"] == "printers"
            queries.append(parameters["col"])
            present = parameters["col"] in columns
            return SimpleNamespace(first=lambda: (1,) if present else None)
        updates.append(sql)
        return MagicMock()

    @asynccontextmanager
    async def nested():
        yield

    conn = SimpleNamespace(execute=AsyncMock(side_effect=execute), begin_nested=MagicMock(side_effect=nested))

    async def add_column(_conn, sql):
        assert _conn is conn
        assert sql == "ALTER TABLE printers ADD COLUMN plugin_id VARCHAR(64) NOT NULL DEFAULT 'bambu'"
        columns.add("plugin_id")

    ddl = AsyncMock(side_effect=add_column)
    monkeypatch.setattr(database, "_safe_execute", ddl)
    await database._migrate_printer_plugin_identity(conn)
    ddl.assert_awaited_once()
    assert queries == (["plugin_id"] if existing_plugin_column else ["plugin_id", "printer_type"])
    if not existing_plugin_column and legacy_column:
        assert len(updates) == 1
        assert "SET plugin_id = TRIM(printer_type)" in updates[0]
        assert "printer_type IS NOT NULL AND TRIM(printer_type) <> ''" in updates[0]
        conn.begin_nested.assert_called_once()
    else:
        assert updates == []
        conn.begin_nested.assert_not_called()
    await database._migrate_printer_plugin_identity(conn)
    assert len(updates) == (1 if not existing_plugin_column and legacy_column else 0)

"""SDK job identity admission and database widths agree on both dialects."""

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.print_log import PrintLogEntry
from backend.tests.unit.test_oidc_icon_migration_pg import _capture_sql


@pytest.mark.asyncio
async def test_existing_postgres_job_identity_columns_are_widened():
    executed = await _capture_sql(is_sqlite_value=False)
    for table in ("print_archives", "print_log_entries"):
        assert f"ALTER TABLE {table} ALTER COLUMN subtask_id TYPE VARCHAR(256)" in executed
    # An older database may not have the run identity column yet. Its later
    # ADD COLUMN must not recreate the narrower width after the attempted ALTER.
    assert "ALTER TABLE print_log_entries ADD COLUMN subtask_id VARCHAR(256)" in executed


@pytest.mark.asyncio
async def test_sqlite_does_not_receive_unsupported_identity_width_alters():
    executed = await _capture_sql(is_sqlite_value=True)
    assert not [statement for statement in executed if "ALTER COLUMN subtask_id" in statement]


def test_new_database_models_use_the_sdk_identity_capacity():
    assert PrintArchive.__table__.c.subtask_id.type.length == 256
    assert PrintLogEntry.__table__.c.subtask_id.type.length == 256

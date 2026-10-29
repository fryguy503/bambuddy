"""Exact native RFID matching never guesses which physical spool is loaded."""

import asyncio
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base metadata
from backend.app.core import database
from backend.app.core.database import Base
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
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import inventory_mode
from backend.app.services.anycubic import AnycubicMQTTClient, spool_matching as native
from backend.app.services.bambu_mqtt import PrinterState


def tray(material="PLA Matte", color="123456FF", index=0, **changes):
    return {
        "id": index,
        "tray_type": material,
        "tray_color": color,
        "anycubic_edit_status": 0,
        "anycubic_status": 5,
        "state": 3,
        **changes,
    }


def boxes(*trays, box_id=0):
    return [{"id": box_id, "tray": list(trays)}]


def spool(material="PLA", subtype="Matte", color="123456FF", **changes):
    return Spool(material=material, subtype=subtype, rgba=color, **changes)


@pytest.fixture
async def case(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_match_lock", asyncio.Lock())
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'native-matching.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        printers = [
            Printer(
                name=f"S1-{index}",
                printer_type="anycubic",
                serial_number=f"RFID-S1-{index}",
                model="Kobra S1",
                ip_address=f"192.0.2.{index}",
                access_code="",
                tls_fingerprint="a" * 64,
            )
            for index in (1, 2)
        ]
        db.add_all(printers)
        await db.commit()
        ids = [printer.id for printer in printers]

    snapshots = {ids[0]: boxes(tray()), ids[1]: []}
    clients = {}
    for printer_id in ids:
        client = MagicMock(spec=AnycubicMQTTClient)
        client.printer_type = "anycubic"
        client.state = PrinterState()
        client.state.connected = True
        client.state.state = "IDLE"
        client.ace_material_snapshot.side_effect = lambda pid=printer_id: copy.deepcopy(snapshots[pid])
        clients[printer_id] = client
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(native.printer_manager, "_clients", clients)
    monkeypatch.setattr(native.printer_manager, "get_client", clients.get)
    monkeypatch.setattr(
        native.printer_manager, "get_all_statuses", lambda: {pid: c.state for pid, c in clients.items()}
    )
    monkeypatch.setattr(native.printer_manager, "get_status", lambda pid: clients[pid].state)
    mode = AsyncMock(return_value=False)
    monkeypatch.setattr(inventory_mode, "spoolman_owns_assignments", mode)
    broadcast = AsyncMock()
    monkeypatch.setattr(native.ws_manager, "broadcast", broadcast)

    async def add(*rows):
        async with factory() as db:
            db.add_all(rows)
            await db.commit()
        return rows

    async def assignments():
        async with factory() as db:
            return list((await db.scalars(select(SpoolAssignment).order_by(SpoolAssignment.id))).all())

    yield SimpleNamespace(
        factory=factory,
        ids=ids,
        clients=clients,
        snapshots=snapshots,
        add=add,
        assignments=assignments,
        broadcast=broadcast,
        mode=mode,
    )
    await engine.dispose()


@pytest.mark.parametrize("material", ["PLA Matte", " pla   matte ", "PLA MATTE"])
def test_complete_material_and_hex_normalize_formatting_only(material):
    assert native.exact_key(material, "#123456") == native.exact_key("PLA Matte", "123456ff")


@pytest.mark.parametrize("other", ["PLA", "PLA Basic", "PLA+", "PLA Silk", "PLA Matte+"])
def test_material_variants_are_distinct(other):
    assert native.exact_key("PLA Matte", "123456FF") != native.exact_key(other, "123456FF")


@pytest.mark.parametrize("color", [None, "", "12345", "1234567", "123456789", "ZZ3456", "##123456", 123456])
def test_malformed_color_does_not_produce_a_match_key(color):
    assert native.exact_key("PLA Matte", color) is None


def test_alpha_is_part_of_exact_color():
    assert native.exact_key("PLA Matte", "12345680") != native.exact_key("PLA Matte", "123456FF")
    assert native.exact_key("PLA Matte", "123456") == native.exact_key("PLA Matte", "123456FF")


def test_inventory_subtype_remains_part_of_material_key():
    assert native.spool_key(spool()) == native.exact_key("PLA Matte", "123456FF")
    assert native.spool_key(spool(material="PLA Matte", subtype=None)) == native.spool_key(spool())
    assert native.spool_key(spool(material="PLA", subtype="PLA Matte")) == native.spool_key(spool())
    assert native.spool_key(spool(subtype=None)) != native.spool_key(spool())


def test_multicolor_inventory_is_not_a_solid_color_match():
    assert native.spool_key(spool(extra_colors="123456FF,ABCDEFff")) is None


@pytest.mark.asyncio
async def test_unique_exact_match_is_local_and_idempotent(case):
    candidate = spool()
    await case.add(candidate)
    await native.observe_status(case.ids[0])
    await native.observe_status(case.ids[0])
    rows = await case.assignments()
    assert len(rows) == 1
    assert (rows[0].spool_id, rows[0].printer_id, rows[0].ams_id, rows[0].tray_id) == (
        candidate.id,
        case.ids[0],
        0,
        0,
    )
    assert rows[0].rfid_match is True
    assert rows[0].fingerprint_type == "PLA Matte"
    assert rows[0].fingerprint_color == "123456FF"
    assert case.broadcast.await_count == 1
    assert case.broadcast.await_args.args[0]["type"] == "spool_auto_assigned"
    for client in case.clients.values():
        assert all(call[0] == "ace_material_snapshot" for call in client.method_calls)
    async with case.factory() as db:
        stored = await db.get(Spool, candidate.id)
        assert stored.tag_uid is None and stored.tray_uuid is None
        assert len((await db.scalars(select(Spool))).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        {"subtype": None},
        {"subtype": "Basic"},
        {"material": "PLA+", "subtype": None},
        {"color": "123457FF"},
        {"color": "12345680"},
        {"extra_colors": "123456FF,ABCDEFff"},
    ],
)
async def test_near_matches_stay_unassigned(case, candidate):
    await case.add(spool(**candidate))
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
async def test_opaque_six_digit_inventory_color_matches(case):
    candidate = spool(color="123456")
    await case.add(candidate)
    await native.observe_status(case.ids[0])
    assert [row.spool_id for row in await case.assignments()] == [candidate.id]


@pytest.mark.asyncio
async def test_duplicate_physical_spools_are_not_resolved_by_fifo_or_weight(case):
    await case.add(spool(weight_used=999), spool(weight_used=0))
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
async def test_duplicate_assigned_elsewhere_still_makes_inventory_ambiguous(case):
    first, second = await case.add(spool(), spool())
    await case.add(SpoolAssignment(spool_id=second.id, printer_id=case.ids[1], ams_id=0, tray_id=0))
    await native.observe_status(case.ids[0])
    assert [row.spool_id for row in await case.assignments()] == [second.id]
    assert first.id != second.id


@pytest.mark.asyncio
async def test_archived_duplicate_does_not_block_active_spool(case):
    active, _archived = await case.add(spool(), spool(archived_at=datetime.now(timezone.utc)))
    await native.observe_status(case.ids[0])
    assert [row.spool_id for row in await case.assignments()] == [active.id]


@pytest.mark.asyncio
async def test_unique_spool_already_assigned_elsewhere_is_not_stolen(case):
    candidate = spool()
    await case.add(candidate)
    await case.add(SpoolAssignment(spool_id=candidate.id, printer_id=case.ids[1], ams_id=0, tray_id=1))
    await native.observe_status(case.ids[0])
    rows = await case.assignments()
    assert [(row.printer_id, row.tray_id) for row in rows] == [(case.ids[1], 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("other_box", [0, 1])
async def test_identical_rfid_in_two_slots_does_not_pick_first(case, other_box):
    await case.add(spool())
    case.snapshots[case.ids[0]] = boxes(tray())
    if other_box == 0:
        case.snapshots[case.ids[0]][0]["tray"].append(tray(index=1))
    else:
        case.snapshots[case.ids[0]].extend(boxes(tray(index=0), box_id=1))
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
async def test_identical_rfid_across_printers_is_ambiguous(case):
    await case.add(spool())
    case.snapshots[case.ids[1]] = boxes(tray())
    await asyncio.gather(*(native.observe_status(printer_id) for printer_id in case.ids))
    assert await case.assignments() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"anycubic_edit_status": 1},
        {"anycubic_edit_status": None},
        {"anycubic_status": 4, "state": 9},
        {"tray_type": ""},
        {"tray_type": "UNKNOWN"},
        {"tray_color": "1234567"},
    ],
)
async def test_only_occupied_identified_rfid_is_eligible(case, changes):
    await case.add(spool())
    case.snapshots[case.ids[0]] = boxes(tray(**changes))
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
async def test_manual_assignment_is_never_replaced_or_unlinked(case):
    candidate, manual = await case.add(spool(), spool(material="PETG", subtype=None, color="FFFFFFFF"))
    await case.add(
        SpoolAssignment(
            spool_id=manual.id,
            printer_id=case.ids[0],
            ams_id=0,
            tray_id=0,
            fingerprint_type="PETG",
            fingerprint_color="FFFFFFFF",
        )
    )
    await native.observe_status(case.ids[0])
    case.snapshots[case.ids[0]] = boxes(tray(anycubic_status=4, state=9, tray_type=""))
    await native.observe_status(case.ids[0])
    assert [row.spool_id for row in await case.assignments()] == [manual.id]
    assert manual.id != candidate.id


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["empty", "material", "color", "non-rfid"])
async def test_automatic_assignment_unlinks_when_reported_spool_changes(case, changed):
    await case.add(spool())
    await native.observe_status(case.ids[0])
    assert len(await case.assignments()) == 1
    changes = {
        "empty": {"tray_type": "", "state": 9, "anycubic_status": 4},
        "material": {"tray_type": "PLA"},
        "color": {"tray_color": "ABCDEFff"},
        "non-rfid": {"anycubic_edit_status": 1},
    }[changed]
    case.snapshots[case.ids[0]] = boxes(tray(**changes))
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
async def test_new_duplicate_inventory_unlinks_only_automatic_match(case):
    await case.add(spool())
    await native.observe_status(case.ids[0])
    assert len(await case.assignments()) == 1
    await case.add(spool())
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute,value", [("subtype", "Silk"), ("rgba", "ABCDEFff")])
async def test_inventory_edits_invalidating_exact_match_unlink_auto_assignment(case, attribute, value):
    candidate = spool()
    await case.add(candidate)
    await native.observe_status(case.ids[0])
    assert len(await case.assignments()) == 1
    async with case.factory() as db:
        stored = await db.get(Spool, candidate.id)
        setattr(stored, attribute, value)
        await db.commit()
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["same-box", "second-box", "other-printer"])
async def test_late_identical_rfid_report_removes_previous_auto_match(case, location):
    await case.add(spool())
    await native.observe_status(case.ids[0])
    assert len(await case.assignments()) == 1
    if location == "same-box":
        case.snapshots[case.ids[0]][0]["tray"].append(tray(index=1))
    elif location == "second-box":
        case.snapshots[case.ids[0]].extend(boxes(tray(), box_id=1))
    else:
        case.snapshots[case.ids[1]] = boxes(tray())
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, []])
async def test_missing_or_stale_snapshot_does_not_unlink_existing_assignment(case, snapshot):
    await case.add(spool())
    await native.observe_status(case.ids[0])
    case.snapshots[case.ids[0]] = snapshot
    await native.observe_status(case.ids[0])
    assert len(await case.assignments()) == 1


@pytest.mark.asyncio
async def test_inventory_owned_by_spoolman_is_untouched(case):
    await case.add(spool())
    case.mode.return_value = True
    await native.observe_status(case.ids[0])
    assert await case.assignments() == []
    case.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_status_pushes_create_exactly_one_assignment(case):
    await case.add(spool())
    await asyncio.gather(*(native.observe_status(case.ids[0]) for _ in range(8)))
    assert len(await case.assignments()) == 1
    assert case.broadcast.await_count == 1


@pytest.mark.asyncio
async def test_upgrade_keeps_existing_assignments_manual(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-matching.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                Printer.__table__.insert().values(
                    id=1,
                    name="S1",
                    serial_number="LEGACY-RFID",
                    printer_type="anycubic",
                    model="Kobra S1",
                    ip_address="192.0.2.1",
                    access_code="",
                )
            )
            await conn.execute(Spool.__table__.insert().values(id=1, material="PLA", subtype="Matte", rgba="123456FF"))
            await conn.execute(
                SpoolAssignment.__table__.insert().values(id=1, spool_id=1, printer_id=1, ams_id=0, tray_id=0)
            )
            await conn.execute(text("ALTER TABLE spool_assignment DROP COLUMN rfid_match"))
            await database.run_migrations(conn)
            await database.run_migrations(conn)
            row = (await conn.execute(text("SELECT spool_id, rfid_match FROM spool_assignment WHERE id = 1"))).one()
            assert row == (1, 0)
    finally:
        await engine.dispose()

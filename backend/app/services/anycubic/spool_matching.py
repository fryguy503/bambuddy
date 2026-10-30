"""Local inventory links from unambiguous native RFID material/color reports.

The native protocol exposes no spool UID. Exact metadata is only a candidate
when both the physical inventory and the reported RFID slots are unique.
Nothing here configures the printer, changes a tag, or creates inventory.
"""

import asyncio
import logging
import re
from collections import Counter, defaultdict
from contextlib import AsyncExitStack

from sqlalchemy import select

from backend.app.core import database
from backend.app.core.websocket import ws_manager
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import inventory_mode, slot_unlink_grace, spool_swaps
from backend.app.services.ams_assignment_lock import get_ams_assignment_lock, get_spool_move_lock
from backend.app.services.printer_manager import printer_manager

from .client import AnycubicMQTTClient

logger = logging.getLogger(__name__)
_match_lock = asyncio.Lock()
_HEX = re.compile(r"[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\Z")
_UNKNOWN = {"", "?", "unknown", "unkonwn", "default"}


def _material(value) -> str:
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def exact_key(material, color) -> tuple[str, str] | None:
    """Compare full types and exact RGBA; case, spacing and '#' are formatting."""
    material = _material(material)
    if material in _UNKNOWN or not isinstance(color, str):
        return None
    color = color.strip().removeprefix("#")
    if not _HEX.fullmatch(color):
        return None
    return material, (color + "FF" if len(color) == 6 else color).upper()


def spool_key(spool: Spool) -> tuple[str, str] | None:
    if spool.extra_colors and spool.extra_colors.strip():
        return None  # One solid RFID color cannot identify a multicolor spool.
    material, subtype = _material(spool.material), _material(spool.subtype)
    if not material or material in _UNKNOWN:
        return None
    if subtype and subtype != material and not material.endswith(" " + subtype):
        material = subtype if subtype.startswith(material + " ") else material + " " + subtype
    return exact_key(material, spool.rgba)


def _slots(boxes) -> dict[tuple[int, int], dict]:
    return {
        (box["id"], tray["id"]): tray
        for box in boxes
        for tray in box["tray"]
        if type(box.get("id")) is int and type(tray.get("id")) is int
    }


def _rfid_key(tray: dict) -> tuple[str, str] | None:
    if (
        type(tray.get("anycubic_edit_status")) is not int
        or tray["anycubic_edit_status"] != 0
        or tray.get("anycubic_status") == 4
        or tray.get("state") != 3
    ):
        return None
    return exact_key(tray.get("tray_type"), tray.get("tray_color"))


def _snapshots(printer_id: int) -> dict[int, list]:
    snapshots = {}
    for pid in {*printer_manager.get_all_statuses(), printer_id}:
        client = printer_manager.get_client(pid)
        if isinstance(client, AnycubicMQTTClient):
            boxes = client.ace_material_snapshot()
            if boxes is not None:
                snapshots[pid] = boxes
    return snapshots


async def observe_status(printer_id: int) -> None:
    """Reconcile only fresh native RFID links; preserve manual assignments."""
    async with _match_lock:
        snapshots = _snapshots(printer_id)
        if printer_id not in snapshots:
            return
        keys = {_rfid_key(tray) for tray in _slots(snapshots[printer_id]).values()} - {None}
        async with database.async_session() as db:
            if await inventory_mode.spoolman_owns_assignments(db):
                return
            # Read before taking spool locks; no transaction waits on another
            # assignment writer while holding SQLite's write lock.
            spools = (await db.scalars(select(Spool).where(Spool.archived_at.is_(None)))).all()
            lock_ids = {spool.id for spool in spools if spool_key(spool) in keys}
            lock_ids.update(
                (
                    await db.scalars(
                        select(SpoolAssignment.spool_id).where(
                            SpoolAssignment.printer_id == printer_id, SpoolAssignment.rfid_match.is_(True)
                        )
                    )
                ).all()
            )

        messages = []
        async with AsyncExitStack() as locks:
            for spool_id in sorted(lock_ids):
                await locks.enter_async_context(get_spool_move_lock("inventory", spool_id))
            await locks.enter_async_context(get_ams_assignment_lock(printer_id))
            # A report, manual move, archive or inventory edit may have arrived
            # while locks were pending. Recheck every decision in a new session.
            snapshots = _snapshots(printer_id)
            if printer_id not in snapshots:
                return
            trays = _slots(snapshots[printer_id])
            reported = Counter(
                key
                for boxes in snapshots.values()
                for tray in _slots(boxes).values()
                if (key := _rfid_key(tray)) is not None
            )
            async with database.async_session() as db:
                if await inventory_mode.spoolman_owns_assignments(db):
                    return
                spools = (await db.scalars(select(Spool).where(Spool.archived_at.is_(None)))).all()
                candidates = defaultdict(list)
                for spool in spools:
                    if (key := spool_key(spool)) is not None:
                        candidates[key].append(spool)
                assignments = list((await db.scalars(select(SpoolAssignment))).all())
                current = {(row.ams_id, row.tray_id): row for row in assignments if row.printer_id == printer_id}
                for slot, row in list(current.items()):
                    if not row.rfid_match or slot not in trays:
                        continue
                    key = _rfid_key(trays[slot])
                    matches = candidates.get(key, [])
                    if (
                        key is not None
                        and key == exact_key(row.fingerprint_type, row.fingerprint_color)
                        and reported[key] == 1
                        and len(matches) == 1
                        and matches[0].id == row.spool_id
                    ):
                        continue
                    await db.delete(row)
                    assignments.remove(row)
                    del current[slot]
                    messages.append(
                        {
                            "type": "spool_assignment_changed",
                            "printer_id": printer_id,
                            "ams_id": slot[0],
                            "tray_id": slot[1],
                        }
                    )
                await db.flush()
                held_spools = {row.spool_id for row in assignments}
                for (box_id, slot_id), tray in trays.items():
                    if (box_id, slot_id) in current:
                        continue
                    key = _rfid_key(tray)
                    matches = candidates.get(key, [])
                    # Count all active spools before excluding assigned ones.
                    # Neither FIFO nor "only one unassigned" proves uniqueness.
                    if key is None or reported[key] != 1 or len(matches) != 1:
                        continue
                    spool = matches[0]
                    if spool.id not in lock_ids or spool.id in held_spools:
                        continue
                    db.add(
                        SpoolAssignment(
                            spool_id=spool.id,
                            printer_id=printer_id,
                            ams_id=box_id,
                            tray_id=slot_id,
                            fingerprint_color=key[1],
                            fingerprint_type=tray["tray_type"],
                            rfid_match=True,
                        )
                    )
                    held_spools.add(spool.id)
                    await spool_swaps.record_assignment(
                        db, printer_id, box_id, slot_id, spool.id, spool_swaps.INVENTORY
                    )
                    slot_unlink_grace.forget_slot(printer_id, box_id, slot_id)
                    messages.append(
                        {
                            "type": "spool_auto_assigned",
                            "printer_id": printer_id,
                            "ams_id": box_id,
                            "tray_id": slot_id,
                            "spool_id": spool.id,
                        }
                    )
                if messages:
                    await db.commit()
        for message in messages:
            await ws_manager.broadcast(message)
            logger.info("Anycubic RFID inventory link changed: %s", message)

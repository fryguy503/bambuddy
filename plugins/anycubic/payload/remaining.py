"""Operator-selected ACE estimates update balances only after print accounting."""

from __future__ import annotations

import logging
import math
from copy import deepcopy
from typing import NoReturn

from sqlalchemy import exists, select, update

from . import inventory
from .persistence import Journal, printer_key, storage_lock

logger = logging.getLogger(__name__)


def _error(code, message) -> NoReturn:
    raise inventory.InventoryAssignmentError(code, message)


def _ace_source(source_id):
    source = inventory._source(source_id)
    if not source.startswith("material:ace:"):
        _error("invalid_source", "Select an ACE material slot with an assigned spool")
    return source


def _expected_spool(spool_id):
    if type(spool_id) is not int or spool_id < 1:
        _error("invalid_spool", "Select the assigned inventory spool")


def _reading(source, sample):
    boxes = sample()
    if not isinstance(boxes, list):
        _error("reading_unavailable", "A fresh ACE reading from an idle printer is required")
    matches = []
    for box in boxes:
        if not isinstance(box, dict) or type(box.get("id")) is not int or not isinstance(box.get("tray"), list):
            continue
        for tray in box.get("tray", []):
            if (
                isinstance(tray, dict)
                and type(tray.get("id")) is int
                and tray["id"] in range(4)
                and source == f"material:ace:{box['id']}:{tray['id']}"
            ):
                matches.append((box, tray))
    if len(matches) != 1:
        _error("reading_unavailable", "The selected ACE slot is unavailable; refresh printer status")
    box, tray = matches[0]
    percent = tray.get("remain")
    if (
        tray.get("state") != 3
        or inventory.exact_key(tray.get("tray_type"), tray.get("tray_color")) is None
        or type(percent) not in (int, float)
        or not math.isfinite(percent)
        or not 0 <= percent <= 100
    ):
        _error("reading_unavailable", "The selected ACE slot has no valid remaining-filament estimate")
    identity = inventory._native_observations([box]).get(source, {}).get("identity")
    return {"percent": float(percent), "identity": identity}


def _link(config, source, expected_spool_id, *, automatic=False, locked=True):
    if locked:
        with storage_lock("inventory"):
            link = deepcopy(inventory._entry(inventory._load(), config)["links"].get(source))
    else:
        # Writers publish with os.replace: this read is an entire old or new
        # JSON snapshot. Once SQL has a write lock, never wait for inventory's
        # OS lock: a retained version may hold it while awaiting that SQL row.
        link = deepcopy(inventory._entry(inventory._load(), config)["links"].get(source))
    if not link or link.get("spool_id") != expected_spool_id or not link.get("token"):
        _error("assignment_changed", "The assigned spool changed; reopen the ACE slot before syncing")
    if automatic and not inventory.remaining_sync_enabled(link):
        _error("sync_disabled", "Automatic ACE remaining synchronization is disabled for this spool assignment")
    return link


def _accounting_pending(config):
    journal = Journal(printer_key(config))
    return bool(journal.unresolved() or journal.pending_accounting())


async def _idle(db, config):
    from backend.app.models.active_print_session import ActivePrintSession
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.usage_tracker import printer_is_charging

    if await inventory._storage_io(_accounting_pending, config):
        _error("print_in_progress", "Wait until the print has ended and its filament usage has been recorded")
    if config.printer_id is not None and (
        printer_is_charging(config.printer_id)
        or await db.scalar(
            select(ActivePrintSession.printer_id).where(ActivePrintSession.printer_id == config.printer_id)
        )
        or await db.scalar(
            select(PrintQueueItem.id)
            .where(PrintQueueItem.printer_id == config.printer_id, PrintQueueItem.status == "printing")
            .limit(1)
        )
    ):
        _error("print_in_progress", "Wait until the print has ended and its filament usage has been recorded")


async def _spool(db, spool_id):
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services.inventory_mode import spoolman_owns_assignments

    if await spoolman_owns_assignments(db):
        _error("inventory_mode", "ACE remaining synchronization requires built-in spool inventory")
    spool = await db.get(Spool, spool_id)
    if spool is None or spool.archived_at is not None:
        _error("spool_unavailable", "The assigned inventory spool is unavailable; refresh inventory")
    if await db.scalar(select(SpoolAssignment.id).where(SpoolAssignment.spool_id == spool_id).limit(1)):
        _error("host_assignment_conflict", "Remove the spool's existing host assignment before syncing it")
    return spool


async def sync_remaining(config, source_id, sample, *, expected_spool_id, automatic=False):
    from backend.app.core.database import async_session
    from backend.app.core.websocket import ws_manager
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services.ams_assignment_lock import get_spool_move_lock

    source = _ace_source(source_id)
    _expected_spool(expected_spool_id)
    if type(automatic) is not bool:
        _error("invalid_setting", "Choose whether to synchronize ACE remaining estimates automatically")
    async with inventory._async_lock, get_spool_move_lock("inventory", expected_spool_id):
        reading = _reading(source, sample)
        link = await inventory._storage_io(lambda: _link(config, source, expected_spool_id, automatic=automatic))
        if link.get("identity") != reading["identity"]:
            _error("assignment_changed", "The ACE material changed; refresh and confirm its spool assignment")
        async with async_session() as db:
            await _idle(db, config)
            spool = await _spool(db, expected_spool_id)
            if automatic and spool.weight_locked:
                _error("weight_locked", "The assigned spool's remaining weight is locked")
            label = spool.label_weight
            if type(label) not in (int, float) or not math.isfinite(label) or label <= 0:
                _error("invalid_label_weight", "Set a valid spool label weight before using an ACE estimate")
            used = round(float(label) * (100 - reading["percent"]) / 100, 1)
            changed = spool.weight_used != used
            if changed:
                query = update(Spool).where(
                    Spool.id == spool.id,
                    Spool.label_weight == label,
                    Spool.weight_used == spool.weight_used,
                    Spool.weight_locked == spool.weight_locked,
                    Spool.archived_at.is_(None),
                    ~exists().where(SpoolAssignment.spool_id == Spool.id),
                )
                updated = await db.scalar(
                    query.values(weight_used=used).returning(Spool.id).execution_options(synchronize_session=False)
                )
                if updated is None:
                    _error("spool_changed", "The spool balance changed; refresh inventory before syncing again")
            # Do not commit a selection whose source, assignment or print state
            # changed while the database was awaiting a lock or reading rows.
            if _reading(source, sample) != reading:
                _error("reading_changed", "The ACE reading changed; refresh the slot before syncing again")
            current = await inventory._storage_io(
                lambda: _link(config, source, expected_spool_id, automatic=automatic, locked=False)
            )
            if current != link:
                _error("assignment_changed", "The assigned spool changed; reopen the ACE slot before syncing")
            await _idle(db, config)
            if changed:
                await db.commit()
    if changed:
        try:
            await ws_manager.broadcast({"type": "inventory_changed"})
        except Exception:
            logger.warning("ACE remaining estimate saved; inventory notification could not be delivered")
    return {
        "state": "synced" if changed else "unchanged",
        "spool_id": expected_spool_id,
        "source_id": source,
        "remaining_percent": reading["percent"],
        "remaining_grams": round(float(label) - used, 1),
        "weight_used": used,
    }


async def set_remaining_sync(config, source_id, enabled, expected_spool_id):
    from backend.app.core.database import async_session

    source = _ace_source(source_id)
    _expected_spool(expected_spool_id)
    if type(enabled) is not bool:
        _error("invalid_setting", "Choose whether to synchronize ACE remaining estimates automatically")
    async with inventory._async_lock:
        async with async_session() as db:
            await _spool(db, expected_spool_id)
        return await inventory._storage_io(_set_remaining_sync, config, source, enabled, expected_spool_id)


def _set_remaining_sync(config, source, enabled, expected_spool_id):
    with storage_lock("inventory"):
        value = inventory._load()
        entry = inventory._entry(value, config)
        link = entry["links"].get(source)
        if not link or link.get("spool_id") != expected_spool_id or not link.get("token"):
            _error("assignment_changed", "The assigned spool changed; reopen the ACE slot before updating its settings")
        observed = entry.get("observed", {}).get(source, {}).get("identity")
        if enabled and (observed is None or link.get("identity") != observed):
            _error(
                "assignment_changed",
                "Refresh ACE status and confirm its spool assignment before enabling automatic sync",
            )
        if inventory.remaining_sync_enabled(link) != enabled or (
            not enabled and ("auto_remaining_sync" in link or "auto_remaining_token" in link)
        ):
            if enabled:
                link["auto_remaining_sync"] = True
                link["auto_remaining_token"] = link["token"]
            else:
                inventory._clear_remaining_sync(link)
            inventory._save(value)
        return deepcopy(entry["links"])


async def sync_remaining_auto(config, sample):
    links = await inventory.snapshot(config)
    results = []
    for source, link in links.items():
        if not source.startswith("material:ace:") or not inventory.remaining_sync_enabled(link):
            continue
        try:
            results.append(
                await sync_remaining(config, source, sample, expected_spool_id=link["spool_id"], automatic=True)
            )
        except inventory.InventoryAssignmentError as exc:
            results.append({"state": "skipped", "source_id": source, "reason": exc.code})
    return results

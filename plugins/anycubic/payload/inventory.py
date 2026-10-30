"""Opaque resource-to-spool links and conservative, idempotent native usage.

RFID reports identify material/color, not a physical UID. Automatic matching
requires uniqueness in both physical inventory and observed native slots.
No core schema or packed vendor tray IDs are introduced by this package.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from contextlib import AsyncExitStack
from uuid import uuid4

from sqlalchemy import select

from .persistence import printer_key, read_json, state_root, storage_lock, write_json

_async_lock = asyncio.Lock()
_UNKNOWN = {"", "?", "unknown", "unkonwn", "default"}


def _load():
    return read_json(state_root() / "inventory.json", {"format_version": 1, "printers": {}})


def _save(value):
    write_json(state_root() / "inventory.json", value)


def _entry(value, config):
    return value.setdefault("printers", {}).setdefault(printer_key(config), {"links": {}, "observed": {}})


def _source(source_id):
    if not isinstance(source_id, str) or not re.fullmatch(r"material:(?:ace:-?\d+:\d+|external)", source_id):
        raise ValueError("Select a material source resource")
    return source_id


def _material(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def exact_key(material, color):
    material = _material(material)
    if material in _UNKNOWN or not isinstance(color, str):
        return None
    color = color.strip().removeprefix("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", color):
        return None
    return material, (color + "FF" if len(color) == 6 else color).upper()


def spool_key(spool):
    if spool.extra_colors and spool.extra_colors.strip():
        return None
    material, subtype = _material(spool.material), _material(spool.subtype)
    if subtype and subtype != material and not material.endswith(" " + subtype):
        material = subtype if subtype.startswith(material + " ") else material + " " + subtype
    return exact_key(material, spool.rgba)


def _native_observations(boxes):
    result = {}
    for box in boxes:
        if not isinstance(box, dict) or type(box.get("id")) is not int:
            continue
        for tray in box.get("tray", []):
            if not isinstance(tray, dict) or type(tray.get("id")) is not int:
                continue
            source = f"material:ace:{box['id']}:{tray['id']}"
            key = exact_key(tray.get("tray_type"), tray.get("tray_color"))
            rfid = (
                type(tray.get("anycubic_edit_status")) is int
                and tray["anycubic_edit_status"] == 0
                and tray.get("anycubic_status") != 4
                and tray.get("state") == 3
            )
            result[source] = {
                "key": list(key) if key and rfid else None,
                "identity": [
                    list(key) if key else None,
                    tray.get("state"),
                    tray.get("anycubic_edit_status"),
                    tray.get("anycubic_status"),
                ],
            }
    return result


async def snapshot(config):
    async with _async_lock:
        with storage_lock("inventory"):
            return dict(_entry(_load(), config)["links"])


async def assign(config, source_id, spool_id):
    from backend.app.core.database import async_session
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services.inventory_mode import spoolman_owns_assignments

    source_id = _source(source_id)
    if spool_id is not None and (type(spool_id) is not int or spool_id < 1):
        raise ValueError("Select an inventory spool or clear the assignment")
    async with _async_lock:
        if spool_id is not None:
            async with async_session() as db:
                if await spoolman_owns_assignments(db):
                    raise ValueError("Package spool links use built-in inventory")
                spool = await db.get(Spool, spool_id)
                if spool is None or spool.archived_at is not None:
                    raise ValueError("Inventory spool is unavailable")
                if await db.scalar(select(SpoolAssignment.id).where(SpoolAssignment.spool_id == spool_id).limit(1)):
                    raise ValueError("Remove the spool's existing host assignment before moving it")
        with storage_lock("inventory"):
            value = _load()
            entry = _entry(value, config)
            if spool_id is None:
                entry["links"].pop(source_id, None)
            else:
                for key, other in value["printers"].items():
                    for source, link in other.get("links", {}).items():
                        if link.get("spool_id") == spool_id and (key, source) != (printer_key(config), source_id):
                            raise ValueError("Remove the spool's existing plugin assignment before moving it")
                entry["links"][source_id] = {
                    "spool_id": spool_id,
                    "token": str(uuid4()),
                    "kind": "manual",
                    "identity": entry.get("observed", {}).get(source_id, {}).get("identity"),
                }
            _save(value)
            return dict(entry["links"])


async def observe(config, native_boxes):
    from backend.app.core.database import async_session
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services.inventory_mode import spoolman_owns_assignments

    if not isinstance(native_boxes, list):
        return
    observed = _native_observations(native_boxes)
    async with _async_lock:
        async with async_session() as db:
            if await spoolman_owns_assignments(db):
                return
            spools = list((await db.scalars(select(Spool).where(Spool.archived_at.is_(None)))).all())
            host_assigned = set((await db.scalars(select(SpoolAssignment.spool_id))).all())
        candidates = defaultdict(list)
        for spool in spools:
            key = spool_key(spool)
            if key:
                candidates[key].append(spool.id)
        with storage_lock("inventory"):
            value = _load()
            entry = _entry(value, config)
            entry["observed"] = observed
            counts = Counter(
                tuple(slot["key"])
                for printer in value["printers"].values()
                for slot in printer.get("observed", {}).values()
                if slot.get("key")
            )
            for source, link in list(entry["links"].items()):
                current = observed.get(source, {})
                if link.get("identity") != current.get("identity"):
                    if link.get("kind") == "rfid":
                        del entry["links"][source]
                    else:
                        link.update(identity=current.get("identity"), token=str(uuid4()))
            for other in value["printers"].values():
                for source, link in list(other.get("links", {}).items()):
                    key_value = other.get("observed", {}).get(source, {}).get("key")
                    key = tuple(key_value) if key_value else None
                    if link.get("kind") == "rfid" and (
                        not key
                        or counts[key] != 1
                        or len(candidates.get(key, [])) != 1
                        or link.get("spool_id") in host_assigned
                    ):
                        del other["links"][source]
            claimed = {
                link["spool_id"] for printer in value["printers"].values() for link in printer.get("links", {}).values()
            }
            for source, item in observed.items():
                key = tuple(item["key"]) if item.get("key") else None
                matches = candidates.get(key, [])
                if source in entry["links"] or not key or counts[key] != 1 or len(matches) != 1:
                    continue
                spool_id = matches[0]
                if spool_id in host_assigned or spool_id in claimed:
                    continue
                entry["links"][source] = {
                    "spool_id": spool_id,
                    "token": str(uuid4()),
                    "kind": "rfid",
                    "identity": item["identity"],
                }
                claimed.add(spool_id)
            _save(value)


def _grams(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 1_000_000 else None


async def capture(config, job, artifact=None):
    from backend.app.core.database import async_session
    from backend.app.models.spool import Spool
    from backend.app.services.inventory_mode import spoolman_owns_assignments

    estimates = (artifact or {}).get("estimates", {}) if isinstance(artifact, dict) else {}
    by_input = {}
    ambiguous_inputs = set()
    for item in estimates.get("anycubic_tool_usage", []) if isinstance(estimates, dict) else []:
        if isinstance(item, dict) and type(item.get("tool")) is int:
            identifier = str(item["tool"])
            if identifier in by_input:
                ambiguous_inputs.add(identifier)
            by_input[identifier] = _grams(item.get("grams"))
    for identifier in ambiguous_inputs:
        by_input[identifier] = None
    result = {"version": 1, "job_id": job.job_id, "state": "captured", "bindings": [], "skipped": []}
    links = await snapshot(config)
    async with async_session() as db:
        if await spoolman_owns_assignments(db):
            return {**result, "state": "skipped", "reason": "spoolman_usage_not_supported"}
        for binding in job.bindings:
            link = links.get(binding.source_id)
            grams = (
                _grams(binding.required_mass_grams)
                if binding.required_mass_grams is not None
                else by_input.get(binding.input_id.removeprefix("T"))
            )
            if not link or grams is None:
                result["skipped"].append(
                    {"input_id": binding.input_id, "reason": "missing_assignment_or_per_input_weight"}
                )
                continue
            spool = await db.get(Spool, link["spool_id"])
            if spool is None or spool.archived_at is not None:
                continue
            result["bindings"].append(
                {
                    "input_id": binding.input_id,
                    "source_id": binding.source_id,
                    "spool_id": spool.id,
                    "token": link["token"],
                    "grams": grams,
                }
            )
    return result


async def complete(config, record, outcome):
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_log import PrintLogEntry
    from backend.app.models.settings import Settings
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spool_usage_history import SpoolUsageHistory
    from backend.app.services.ams_assignment_lock import get_spool_move_lock
    from backend.app.services.inventory_mode import spoolman_owns_assignments
    from backend.app.services.print_cost_estimate import default_cost_per_kg
    from backend.app.services.usage_tracker import _charge_spool

    job_id = record.get("job_id")
    if not isinstance(job_id, str):
        raise ValueError("Usage requires a durable job identity")
    usage = record.get("usage_snapshot") or {}
    valid_usage = (
        isinstance(usage, dict)
        and type(usage.get("version")) is int
        and usage["version"] == 1
        and usage.get("job_id") == job_id
        and usage.get("state") == "captured"
    )
    bindings = usage.get("bindings", []) if valid_usage else []
    marker_key = "anycubic:usage:" + hashlib.sha256((printer_key(config) + "\0" + job_id).encode()).hexdigest()
    async with _async_lock, AsyncExitStack() as locks:
        for spool_id in sorted({item["spool_id"] for item in bindings}):
            await locks.enter_async_context(get_spool_move_lock("inventory", spool_id))
        with storage_lock("inventory"):
            links = _entry(_load(), config)["links"]
            async with async_session() as db:
                if await db.scalar(select(Settings.id).where(Settings.key == marker_key)):
                    return {"state": "already_recorded"}
                # The marker and consumption commit in the same DB transaction.
                # Its unique key is also the cross-process idempotency guard.
                from sqlalchemy.exc import IntegrityError

                try:
                    marker = Settings(key=marker_key, value="{}")
                    db.add(marker)
                    await db.flush()
                except IntegrityError:
                    await db.rollback()
                    return {"state": "already_recorded"}
                result = {"state": "skipped", "outcome": outcome, "usage": []}
                requested_archive_id = record.get("archive_id")
                run_query = (
                    select(PrintLogEntry.archive_id)
                    .where(
                        PrintLogEntry.printer_id == config.printer_id,
                        PrintLogEntry.subtask_id == job_id,
                        PrintLogEntry.archive_id.is_not(None),
                    )
                    .execution_options(include_open_runs=True)
                )
                if requested_archive_id is not None:
                    run_query = run_query.where(PrintLogEntry.archive_id == requested_archive_id)
                linked_archive_id = await db.scalar(run_query.order_by(PrintLogEntry.id.desc()).limit(1))
                archive = await db.get(PrintArchive, linked_archive_id) if linked_archive_id is not None else None
                if archive is None:
                    archive_query = select(PrintArchive).where(
                        PrintArchive.printer_id == config.printer_id, PrintArchive.subtask_id == job_id
                    )
                    if requested_archive_id is not None:
                        archive_query = archive_query.where(PrintArchive.id == requested_archive_id)
                    archive = await db.scalar(archive_query.order_by(PrintArchive.id.desc()).limit(1))
                if outcome == "completed" and bindings and archive is None:
                    await db.rollback()
                    return {"state": "deferred", "reason": "archive_unavailable"}
                if outcome == "completed" and archive is not None and not await spoolman_owns_assignments(db):
                    totals = defaultdict(float)
                    for binding in bindings:
                        link = links.get(binding["source_id"], {})
                        grams = _grams(binding.get("grams"))
                        if (
                            link.get("spool_id") == binding["spool_id"]
                            and link.get("token") == binding["token"]
                            and grams
                        ):
                            totals[binding["spool_id"]] += grams
                    fallback_cost = await default_cost_per_kg(db)
                    for spool_id, grams in totals.items():
                        spool = await db.get(Spool, spool_id)
                        if spool is None or spool.archived_at is not None:
                            continue
                        if await db.scalar(
                            select(SpoolAssignment.id).where(SpoolAssignment.spool_id == spool_id).limit(1)
                        ):
                            continue
                        if not await _charge_spool(db, spool, grams):
                            continue
                        price = spool.cost_per_kg if spool.cost_per_kg is not None else fallback_cost
                        cost = (
                            round(grams * price / 1000, 4)
                            if type(price) in (int, float) and math.isfinite(price) and price >= 0
                            else None
                        )
                        db.add(
                            SpoolUsageHistory(
                                spool_id=spool_id,
                                printer_id=config.printer_id,
                                archive_id=archive.id,
                                print_name=archive.print_name,
                                weight_used=round(grams, 1),
                                percent_used=round(grams / spool.label_weight * 100) if spool.label_weight else 0,
                                status="completed",
                                cost=cost,
                            )
                        )
                        result["usage"].append({"spool_id": spool_id, "grams": grams})
                    result["state"] = "recorded" if result["usage"] else "skipped"
                marker.value = json.dumps(result, allow_nan=False)
                await db.commit()
                return result

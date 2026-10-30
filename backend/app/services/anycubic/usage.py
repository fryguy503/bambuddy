"""Estimated usage for identity-confirmed native queue prints.

The queue owns the durable print transition and commits this work with it.
Only explicit per-tool grams and unchanged physical spool assignments qualify;
neither ACE percentage estimates nor Bambu tray encodings are consumption.
"""

import json
import math

from sqlalchemy import select, update

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import inventory_mode
from backend.app.services.print_cost_estimate import default_cost_per_kg
from backend.app.services.usage_tracker import _charge_spool

SNAPSHOT_KEY = "usage_snapshot"
_HELD_STATES = {"submitting", "accepted", "unknown", "running"}


def _grams(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1_000_000:
        return None
    return float(value)


def _timestamp(value):
    return value.isoformat() if value is not None else None


def _result(reason, *, state="skipped", usage=None, skipped=None):
    return {"state": state, "reason": reason, "usage": usage or [], "skipped": skipped or []}


async def capture_usage(db, printer, artifact, options):
    """Capture under the assignment lock, held by the caller through intent commit."""
    snapshot = {
        "version": 1,
        "printer_id": printer.id,
        "mode": "inventory",
        "state": "captured",
        "tools": [],
        "skipped": [],
        "invalidated_slots": [],
    }
    if await inventory_mode.spoolman_owns_assignments(db):
        snapshot.update(mode="spoolman", state="skipped", reason="spoolman_usage_not_supported")
        return snapshot
    estimates = getattr(artifact, "estimates", None)
    amounts = estimates.get("anycubic_tool_usage") if isinstance(estimates, dict) else None
    if not isinstance(amounts, list):
        snapshot.update(state="skipped", reason="no_reliable_per_tool_weight")
        return snapshot
    planned = {}
    for entry in amounts:
        if not isinstance(entry, dict) or type(entry.get("tool")) is not int or not 0 <= entry["tool"] <= 255:
            snapshot.update(state="skipped", reason="invalid_per_tool_weight")
            return snapshot
        raw_grams = entry.get("grams")
        grams = 0.0 if type(raw_grams) in (int, float) and raw_grams == 0 else _grams(raw_grams)
        tool = entry["tool"]
        if tool in planned or grams is None:
            snapshot.update(state="skipped", reason="invalid_per_tool_weight")
            return snapshot
        planned[tool] = grams
    required = {filament.tool for filament in artifact.filaments}
    mappings = {row["tool"]: row for row in options.get("filament_mapping", [])}
    rows = (
        await db.execute(
            select(SpoolAssignment, Spool)
            .join(Spool, Spool.id == SpoolAssignment.spool_id)
            .where(SpoolAssignment.printer_id == printer.id, Spool.archived_at.is_(None))
        )
    ).all()
    assigned = {(assignment.ams_id, assignment.tray_id): (assignment, spool) for assignment, spool in rows}
    for tool in sorted(required):
        if tool not in planned:
            snapshot["skipped"].append({"tool": tool, "reason": "no_reliable_per_tool_weight"})
            continue
        if planned[tool] == 0:
            continue
        if options.get("use_ace"):
            choice = mappings.get(tool)
            slot = (choice["box_id"], choice["slot_id"]) if choice else None
        else:
            # The shared inventory's explicit external-spool assignment. No
            # ACE slot is inferred for an external print or an unassigned spool.
            slot = (255, 0) if len(required) == 1 else None
        if slot is None:
            snapshot["skipped"].append({"tool": tool, "reason": "spool_not_assigned_at_start"})
            continue
        match = assigned.get(slot)
        if match is None or match[0].created_at is None:
            snapshot["skipped"].append({"tool": tool, "reason": "spool_not_assigned_at_start"})
            continue
        assignment, spool = match
        snapshot["tools"].append(
            {
                "tool": tool,
                "grams": planned[tool],
                "box_id": slot[0],
                "slot_id": slot[1],
                "assignment_id": assignment.id,
                "assigned_at": _timestamp(assignment.created_at),
                "spool_id": spool.id,
            }
        )
    if not snapshot["tools"]:
        snapshot.update(state="skipped", reason="no_assigned_spool_usage")
    return snapshot


async def invalidate_assignment(db, printer_id, box_id, slot_id):
    """Keep even A→B→A assignment changes in the held journal, in the writer's transaction.

    Assignment IDs and timestamps alone cannot prove continuity: SQLite can
    reuse a deleted ID and its server timestamp has one-second resolution.
    The shared assignment hook calls this for every native assignment event.
    """
    rows = (
        (
            await db.execute(
                select(PrintQueueItem.id)
                .join(Printer, Printer.id == PrintQueueItem.printer_id)
                .where(
                    PrintQueueItem.printer_id == printer_id,
                    Printer.printer_type == "anycubic",
                    PrintQueueItem.anycubic_dispatch.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for item_id in rows:
        for _attempt in range(4):
            raw = await db.scalar(select(PrintQueueItem.anycubic_dispatch).where(PrintQueueItem.id == item_id))
            try:
                record = json.loads(raw or "null")
            except (TypeError, ValueError):
                break
            if not isinstance(record, dict) or record.get("state") not in _HELD_STATES:
                break
            snapshot = record.get(SNAPSHOT_KEY)
            if not isinstance(snapshot, dict) or snapshot.get("mode") != "inventory":
                break
            if not any(
                (entry.get("box_id"), entry.get("slot_id")) == (box_id, slot_id) for entry in snapshot.get("tools", [])
            ):
                break
            slots = snapshot.setdefault("invalidated_slots", [])
            if [box_id, slot_id] in slots:
                break
            slots.append([box_id, slot_id])
            changed = await db.execute(
                update(PrintQueueItem)
                .where(PrintQueueItem.id == item_id, PrintQueueItem.anycubic_dispatch == raw)
                .values(anycubic_dispatch=json.dumps(record))
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount == 1:
                break
        else:
            # Do not let an assignment commit without its native ambiguity
            # note when concurrent status writes prevented the CAS repeatedly.
            raise RuntimeError("The print changed while assigning this spool; retry the assignment")


async def complete_usage(db, printer_id, archive, snapshot):
    """Charge a successful native run after its journal CAS, without committing."""
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        return _result("no_start_snapshot")
    if snapshot.get("printer_id") != printer_id:
        return _result("snapshot_printer_mismatch")
    if snapshot.get("mode") != "inventory":
        return _result("spoolman_usage_not_supported")
    if await inventory_mode.spoolman_owns_assignments(db):
        return _result("inventory_mode_changed")
    if snapshot.get("state") != "captured":
        return _result(snapshot.get("reason") or "no_assigned_spool_usage", skipped=snapshot.get("skipped"))
    if archive is None or archive.id is None:
        return _result("archive_unavailable")
    if archive.status != "completed":
        return _result("print_not_completed")
    existing = await db.scalar(select(SpoolUsageHistory.id).where(SpoolUsageHistory.archive_id == archive.id).limit(1))
    if existing is not None:
        return _result("usage_already_recorded", state="recorded")
    skipped = list(snapshot.get("skipped", []))
    grouped = {}
    for entry in snapshot.get("tools", []):
        slot = [entry["box_id"], entry["slot_id"]]
        if slot in snapshot.get("invalidated_slots", []):
            skipped.append({"tool": entry["tool"], "reason": "assignment_changed_during_print"})
            continue
        assignment = await db.get(SpoolAssignment, entry["assignment_id"])
        if (
            assignment is None
            or assignment.printer_id != printer_id
            or (assignment.ams_id, assignment.tray_id) != tuple(slot)
            or assignment.spool_id != entry["spool_id"]
            or _timestamp(assignment.created_at) != entry["assigned_at"]
        ):
            skipped.append({"tool": entry["tool"], "reason": "assignment_changed_during_print"})
            continue
        spool = await db.get(Spool, entry["spool_id"])
        grams = _grams(entry.get("grams"))
        if spool is None or spool.archived_at is not None or grams is None:
            skipped.append({"tool": entry["tool"], "reason": "spool_or_estimate_unavailable"})
            continue
        group = grouped.setdefault(spool.id, {"spool": spool, "grams": 0.0, "tools": []})
        group["grams"] += grams
        group["tools"].append(entry)
    fallback_cost = await default_cost_per_kg(db)
    fallback_cost = fallback_cost if math.isfinite(fallback_cost) and fallback_cost >= 0 else 0.0
    usage = []
    charged_grams = 0.0
    for group in grouped.values():
        spool, grams = group["spool"], group["grams"]
        if not await _charge_spool(db, spool, grams):
            skipped.extend({"tool": item["tool"], "reason": "spool_unavailable"} for item in group["tools"])
            continue
        charged_grams += grams
        rate = spool.cost_per_kg
        rate = rate if rate is not None and math.isfinite(rate) and rate >= 0 else fallback_cost
        cost = round(grams / 1000.0 * rate, 2)
        percent = round(grams / (spool.label_weight or 1000) * 100)
        db.add(
            SpoolUsageHistory(
                spool_id=spool.id,
                printer_id=printer_id,
                print_name=archive.print_name or archive.filename,
                archive_id=archive.id,
                weight_used=round(grams, 1),
                percent_used=percent,
                status="completed",
                cost=cost,
            )
        )
        usage.append(
            {
                "spool_id": spool.id,
                "weight_used": round(grams, 1),
                "percent_used": percent,
                "cost": cost,
                "material": spool.material,
                "ams_id": group["tools"][0]["box_id"],
                "tray_id": group["tools"][0]["slot_id"],
                "tools": [item["tool"] for item in group["tools"]],
            }
        )
    if usage:
        total_grams = _grams(archive.filament_used_grams) or charged_grams
        archive.cost = round(
            sum(item["cost"] for item in usage) + max(0.0, total_grams - charged_grams) / 1000.0 * fallback_cost, 2
        )
        return _result(
            "some_filaments_not_charged" if skipped else None,
            state="partial" if skipped else "recorded",
            usage=usage,
            skipped=skipped,
        )
    return _result("no_unchanged_assigned_spool", skipped=skipped)

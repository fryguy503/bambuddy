"""Minimal public views of durable pending dispatches; never grants replay."""

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .persistence import Journal, printer_key


class DispatchHold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=4096)
    status: Literal["intent", "submitting", "publishing", "unknown", "accepted", "running"]
    native_filename: str | None = None
    created_at: datetime | None = None


class DispatchHoldsResponse(BaseModel):
    printer_id: int
    holds: list[DispatchHold]


def summarize(records: list[dict]) -> list[dict]:
    result = []
    for record in records:
        if record.get("status") not in {"intent", "submitting", "publishing", "unknown", "accepted", "running"}:
            continue
        filename = record.get("native_filename")
        if isinstance(filename, str):
            filename = PurePosixPath(filename.replace("\\", "/")).name
            if len(filename) > 255 or any(ord(character) < 32 for character in filename):
                filename = None
        else:
            filename = None
        created = record.get("created_at")
        try:
            created = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else None
        except ValueError:
            created = None
        result.append(
            DispatchHold(
                job_id=record["job_id"], status=record["status"], native_filename=filename, created_at=created
            ).model_dump(mode="json")
        )
    return result


def read(config) -> list[dict]:
    return summarize(Journal(printer_key(config)).unresolved())


def rejection_message(holds: list[dict]) -> str:
    # Display labels are bounded; the read-only status endpoint and enum retain
    # exact opaque IDs for explicitly authorized recovery.
    labels = [
        "".join(character if ord(character) >= 32 else " " for character in hold["job_id"])[:160]
        + " ("
        + hold["status"]
        + ")"
        for hold in holds[:4]
    ]
    suffix = f" and {len(holds) - 4} more" if len(holds) > 4 else ""
    return (
        "Previous native dispatch is still held: "
        + ", ".join(labels)
        + suffix
        + ". Inspect the printer and Advanced controls before explicitly resolving a held job; it will not be resent automatically."
    )

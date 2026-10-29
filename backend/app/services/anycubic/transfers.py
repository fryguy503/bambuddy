"""Upload progress and receipts, following the app's in-process job pattern.

No automatic replay or print-start action. A restart loses progress, so callers
must inspect the printer's catalog before deciding to submit another transfer.
"""

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.core.tasks import spawn_background_task

from .files import GcodeArtifact, upload_gcode
from .protocol import AnycubicError

ACTIVE_PHASES = {"uploading", "verifying"}


@dataclass
class Transfer:
    id: str
    printer_id: int
    filename: str
    size: int
    phase: str = "uploading"
    sent: int = 0
    message: str | None = None
    created: float = field(default_factory=time.monotonic, repr=False)
    cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    staging: Path | None = field(default=None, repr=False)
    artifact_path: Path | None = field(default=None, repr=False)
    attempted: bool = field(default=False, repr=False)

    def public(self):
        return {
            "id": self.id,
            "printer_id": self.printer_id,
            "filename": self.filename,
            "size": self.size,
            "sent": self.sent,
            "phase": self.phase,
            "message": self.message,
        }


class Transfers:
    def __init__(self):
        self.jobs = {}

    def prune(self):
        for key, job in list(self.jobs.items()):
            if job.phase not in ACTIVE_PHASES and time.monotonic() - job.created > 600:
                self.jobs.pop(key, None)

    def active(self, printer_id):
        return any(job.printer_id == printer_id and job.phase in ACTIVE_PHASES for job in self.jobs.values())

    def start(self, printer_id, client, artifact: GcodeArtifact, *, staging: Path | None = None):
        self.prune()
        if self.active(printer_id) or len(self.jobs) >= 64:
            raise AnycubicError("A transfer is already active or the transfer history is full; try again later")
        job = Transfer(
            uuid.uuid4().hex, printer_id, artifact.filename, artifact.size, staging=staging, artifact_path=artifact.path
        )
        self.jobs[job.id] = job
        job.task = spawn_background_task(self._run(job, client, artifact, staging), name="anycubic-file-upload")
        return job

    async def _run(self, job, client, artifact, staging):
        try:
            context = await asyncio.to_thread(client.file_session, require_upload=True)

            def progress(sent):
                job.sent = min(sent, job.size)

            job.attempted = True
            await upload_gcode(client, context, artifact, progress=progress, cancelled=job.cancel)
            job.phase = "verifying"
            keyword = artifact.filename.rsplit("-", 1)[-1]
            for attempt in range(3):
                if job.cancel.is_set() or not client.file_session_valid(context):
                    raise AnycubicError("The transfer session ended; inspect the printer file list")
                catalog = await asyncio.to_thread(client.list_files, keyword=keyword)
                if any(
                    row["name"] == job.filename and not row["is_dir"] and row["size"] == job.size
                    for row in catalog["files"]
                ):
                    job.phase = "completed"
                    return
                if attempt < 2:
                    await asyncio.sleep(1)
            job.phase = "uploaded_unverified"
            job.message = "The printer acknowledged the upload; refresh its file list to confirm it"
        except asyncio.CancelledError:
            job.phase = "unknown" if job.attempted else "cancelled"
            job.message = "Transfer stopped; check the printer file list before retrying"
            raise
        except AnycubicError as exc:
            job.phase = "uploaded_unverified" if job.phase == "verifying" else "unknown" if job.attempted else "failed"
            job.message = str(exc)  # Only the protocol's fixed, secret-free errors.
        except Exception:
            job.phase = "uploaded_unverified" if job.phase == "verifying" else "unknown" if job.attempted else "failed"
            job.message = "Transfer could not be confirmed; check the printer file list before retrying"
        finally:
            self._cleanup(job)

    @staticmethod
    def _cleanup(job):
        if job.staging is not None and job.artifact_path is not None:
            with contextlib.suppress(OSError):
                job.artifact_path.unlink(missing_ok=True)
                job.staging.rmdir()

    def get(self, printer_id, job_id):
        self.prune()
        job = self.jobs.get(job_id)
        return job if job is not None and job.printer_id == printer_id else None

    async def stop(self, job):
        if job.phase in ACTIVE_PHASES and job.task is not None:
            job.cancel.set()
            job.task.cancel()
            try:
                await job.task
            except asyncio.CancelledError:
                pass
            finally:
                if job.phase in ACTIVE_PHASES:
                    job.phase = "unknown" if job.attempted else "cancelled"
                    job.message = "Transfer stopped; check the printer file list before retrying"
                self._cleanup(job)
        return job.public()


transfers = Transfers()

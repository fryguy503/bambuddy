"""Bounded, upload-only transfers through the selected printer provider."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from backend.app.printers.api import operation_lease, printer_api
from backend.app.printers.contracts import PrinterConfig, PrinterPlugin, PrinterPluginError
from backend.app.printers.registry import current_registry, provider_context

logger = logging.getLogger(__name__)


@dataclass
class UploadJob:
    id: str
    printer_id: int
    filename: str
    size: int
    path: Path | None = None
    phase: str = "receiving"
    sent: int = 0
    error: str | None = None
    task: asyncio.Task | None = None
    lease: Any = None
    cancel_requested: bool = False
    lock: RLock = field(default_factory=RLock)

    def describe(self):
        with self.lock:
            return {
                "id": self.id,
                "printer_id": self.printer_id,
                "filename": self.filename,
                "size": self.size,
                "sent": self.sent,
                "phase": self.phase,
                "error": self.error,
            }


class PrinterFileUploads:
    max_active = 4
    max_history = 256

    def __init__(self):
        self.jobs: OrderedDict[tuple[int, str], UploadJob] = OrderedDict()

    def require_capacity(self, printer_id: int):
        active = [
            job
            for job in self.jobs.values()
            if job.phase == "receiving" or job.task is not None and not job.task.done()
        ]
        if len(active) >= self.max_active or any(job.printer_id == printer_id for job in active):
            raise PrinterPluginError("A printer file upload is already active; let that transfer finish")

    def get(self, printer_id: int, job_id: str):
        return self.jobs.get((printer_id, job_id))

    def _prune(self):
        for key, job in list(self.jobs.items()):
            if len(self.jobs) <= self.max_history:
                break
            if job.phase != "receiving" and (job.task is None or job.task.done()):
                del self.jobs[key]

    def reserve(self, printer_id: int, job_id: str, filename: str, size: int):
        if self.get(printer_id, job_id) is not None:
            raise PrinterPluginError("This upload ID was already used; inspect its status instead of resending it")
        self.require_capacity(printer_id)
        job = UploadJob(job_id, printer_id, filename, size)
        self.jobs[(printer_id, job_id)] = job
        self._prune()
        return job

    def start(self, config: PrinterConfig, provider: PrinterPlugin, path: Path, filename: str, job_id: str):
        if config.printer_id is None:
            raise PrinterPluginError("Printer uploads require a configured printer")
        job = self.get(config.printer_id, job_id)
        if job is None or job.phase != "receiving" or job.cancel_requested or job.task is not None:
            raise PrinterPluginError("This upload was cancelled or already accepted; inspect its status")
        job.path, job.size = path, path.stat().st_size
        job.lease = operation_lease(config.printer_id)
        job.lease.__enter__()
        registry = current_registry()
        reference = f"file-upload:{config.printer_id}:{job.id}"
        try:
            registry.retain_runtime(provider, reference, config.printer_id, "file upload in progress")
        except BaseException:
            job.lease.__exit__(None, None, None)
            raise

        def cleanup(_task=None):
            if job.lease is None:
                return
            lease, job.lease = job.lease, None
            registry.release_runtime(reference)
            lease.__exit__(None, None, None)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Unable to remove temporary printer upload %s", job.id)
            if job.phase in {"uploading", "verifying", "cancelling"}:
                job.phase = "cancelled"

        async def transfer():
            def progress(sent, total):
                with job.lock:
                    job.sent = max(job.sent, min(job.size, max(0, int(sent))))
                    if job.sent == job.size and not job.cancel_requested:
                        job.phase = "verifying"

            try:
                with provider_context(config, provider):
                    result = await printer_api.files(
                        config,
                        "upload_file_async",
                        config.ip_address,
                        config.access_code,
                        path,
                        filename,
                        printer_model=config.model,
                        progress_callback=progress,
                    )
                if result is not True:
                    raise PrinterPluginError("The printer did not confirm the file upload")
                with job.lock:
                    job.phase, job.sent = "completed", job.size
            except asyncio.CancelledError:
                job.phase = "cancelled"
                raise
            except Exception:
                logger.exception("Printer %s file upload %s failed", config.printer_id, job.id)
                # Transport exceptions can include credential-bearing URLs.
                job.phase, job.error = (
                    "failed",
                    "The printer did not confirm the upload. Check its file list before retrying.",
                )
            finally:
                cleanup()

        # Providers advertising file_upload must not return cancellation until
        # their native I/O has unwound; executor workers need an explicit stop
        # handshake. The lease and staged bytes remain owned until that point.
        job.phase = "uploading"
        job.task = asyncio.create_task(transfer(), name="printer-file-upload:" + job.id)
        job.task.add_done_callback(cleanup)
        return job

    async def cancel(self, printer_id: int, job_id: str):
        job = self.get(printer_id, job_id)
        if job is None:
            # DELETE can win the race with multipart parsing or staging. The
            # later POST must encounter this tombstone and never reach native I/O.
            job = UploadJob(job_id, printer_id, "", 0, phase="cancelled", cancel_requested=True)
            self.jobs[(printer_id, job_id)] = job
            self._prune()
        elif job.task is not None and not job.task.done():
            if not job.cancel_requested:
                job.cancel_requested, job.phase = True, "cancelling"
                job.task.cancel()
            await asyncio.shield(asyncio.gather(job.task, return_exceptions=True))
        elif job.phase == "receiving":
            job.cancel_requested, job.phase = True, "cancelled"
        return job

    async def shutdown(self):
        tasks = [job.task for job in self.jobs.values() if job.task is not None and not job.task.done()]
        for job in self.jobs.values():
            if job.phase == "receiving":
                job.phase, job.cancel_requested = "cancelled", True
            elif job.task is not None and not job.task.done() and not job.cancel_requested:
                job.cancel_requested, job.phase = True, "cancelling"
                job.task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


printer_file_uploads = PrinterFileUploads()

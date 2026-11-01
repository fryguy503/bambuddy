"""Bound expensive, read-only artifact inspection away from status workers."""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4
from weakref import WeakKeyDictionary

from backend.app.printers.api import operation_lease
from backend.app.printers.contracts import PrinterConfig, PrinterPlugin, PrinterPluginError
from backend.app.printers.registry import current_registry, plugin_for, provider_context
from backend.app.printers.sdk import JobArtifact, JobDescription, validate_job_description

INSPECTION_TIMEOUT_SECONDS = 60.0
ADMISSION_TIMEOUT_SECONDS = 5.0
MAX_WAITING_INSPECTIONS = 16
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="printer-inspection")


class JobInspectionBusy(Exception):
    """Inspection capacity is occupied; the printer itself may be healthy."""


@dataclass
class _LoopState:
    slot: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    waiting: int = 0


_loops: WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = WeakKeyDictionary()
_running: set[asyncio.Task] = set()


async def _in_worker(callback):
    future = asyncio.get_running_loop().run_in_executor(_executor, copy_context().run, callback)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # A synchronous plugin cannot be cancelled. Even process shutdown must
        # keep its runtime reference and capacity until its thread has exited.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not future.cancelled():
            future.exception()
        raise


async def _inspect(config: PrinterConfig, plugin: PrinterPlugin, artifact: JobArtifact, path: Path, state: _LoopState):
    registry = current_registry()
    reference = "inspection:" + uuid4().hex
    try:
        with operation_lease(config.printer_id):
            if plugin_for(config) is not plugin:
                raise PrinterPluginError("Printer support changed; retry reading print requirements")
            registry.retain_runtime(plugin, reference, config.printer_id, "reading print requirements")
            try:
                with provider_context(config, plugin):
                    if inspect.iscoroutinefunction(plugin.describe_job):
                        result = await plugin.describe_job(config, artifact, path)
                    else:
                        result = await _in_worker(lambda: plugin.describe_job(config, artifact, path))
                        if inspect.isawaitable(result):
                            result = await result

                    # Inspect first, then obtain the current descriptor. A large
                    # file must not validate against hardware observed a minute ago.
                    return await _in_worker(lambda: validate_job_description(plugin.describe_device(config), result))
            finally:
                registry.release_runtime(reference)
    finally:
        state.slot.release()


def _finished(task: asyncio.Task) -> None:
    _running.discard(task)
    if not task.cancelled():
        task.exception()  # Consume failures after an HTTP timeout or cancellation.


async def inspect_job(
    config: PrinterConfig, plugin: PrinterPlugin, artifact: JobArtifact, path: Path
) -> JobDescription:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + INSPECTION_TIMEOUT_SECONDS
    state = _loops.setdefault(loop, _LoopState())
    if state.waiting >= MAX_WAITING_INSPECTIONS:
        raise JobInspectionBusy("File inspection is busy; retry reading print requirements shortly")
    state.waiting += 1
    try:
        try:
            await asyncio.wait_for(state.slot.acquire(), min(ADMISSION_TIMEOUT_SECONDS, INSPECTION_TIMEOUT_SECONDS))
        except TimeoutError as exc:
            raise JobInspectionBusy("File inspection is busy; retry reading print requirements shortly") from exc
    finally:
        state.waiting -= 1

    remaining = deadline - loop.time()
    if remaining <= 0:
        state.slot.release()
        raise TimeoutError("Reading print requirements timed out")
    task = asyncio.create_task(_inspect(config, plugin, artifact, path, state))
    _running.add(task)
    task.add_done_callback(_finished)
    # Cancellation belongs to this HTTP request, not to a thread already reading
    # the file. The inspection task owns its slot, lease and runtime reference.
    return await asyncio.wait_for(asyncio.shield(task), remaining)

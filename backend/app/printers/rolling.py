"""Refresh data observers immediately while retaining handlers for existing work."""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, update

from backend.app.printers.contracts import PrinterConfig, PrinterPluginError

if TYPE_CHECKING:
    from backend.app.printers.registry import RuntimeProvider

logger = logging.getLogger(__name__)
_admission_lock = RLock()
_applying: set[int] = set()
_leases: dict[int, int] = {}


class PrinterUpdateInProgress(PrinterPluginError):
    """This printer is briefly publishing its replacement observer."""


@contextmanager
def operation_lease(printer_id: int | None):
    """Admit work atomically with the cutover gate, across any awaits inside it."""
    if printer_id is None:
        yield
        return
    with _admission_lock:
        if printer_id in _applying:
            raise PrinterUpdateInProgress("This printer is applying a plugin update; retry shortly")
        _leases[printer_id] = _leases.get(printer_id, 0) + 1
    try:
        yield
    finally:
        with _admission_lock:
            remaining = _leases.get(printer_id, 1) - 1
            if remaining:
                _leases[printer_id] = remaining
            else:
                _leases.pop(printer_id, None)


def is_applying(printer_id: int | None) -> bool:
    with _admission_lock:
        return printer_id in _applying


def _begin_cutover(printer_id: int) -> bool:
    with _admission_lock:
        if printer_id in _applying:
            return False
        _applying.add(printer_id)
        return True


def _end_cutover(printer_id: int) -> None:
    with _admission_lock:
        _applying.discard(printer_id)


@dataclass
class _Target:
    candidate: RuntimeProvider
    printers: dict[int, dict[str, Any]] = field(default_factory=dict)


class RolloutCoordinator:
    def __init__(self, *, session_factory=None, manager=None, scheduler=None, registry=None):
        self._session_factory = session_factory
        self._manager = manager
        self._scheduler = scheduler
        self._registry = registry
        self._targets: dict[str, _Target] = {}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self.poll_seconds = 2.0
        self.connection_timeout = 20.0
        self.max_parallel = 4

    def _services(self):
        from backend.app.core.database import async_session
        from backend.app.printers.registry import registry
        from backend.app.services.print_scheduler import scheduler
        from backend.app.services.printer_manager import printer_manager

        return (
            self._session_factory or async_session,
            self._manager or printer_manager,
            self._scheduler or scheduler,
            self._registry or registry,
        )

    async def request(self, plugin_id: str, candidate: RuntimeProvider) -> None:
        """Queue a validated selection without waiting for any printer to finish."""
        from backend.app.models.printer import Printer

        factory, manager, _, registry = self._services()
        async with factory() as db:
            printers = list((await db.scalars(select(Printer).where(Printer.plugin_id == plugin_id))).all())
        target = _Target(candidate)
        for printer in printers:
            self._row(target, printer, manager, registry)
        self._targets[plugin_id] = target
        self._wake.set()

    @staticmethod
    def _provider(printer, manager, registry):
        client = manager.get_client(printer.id)
        if client is not None:
            return client._plugin
        try:
            return registry.provider_for(PrinterConfig.from_printer(printer))
        except PrinterPluginError:
            return None

    def _row(self, target, printer, manager, registry):
        current = self._provider(printer, manager, registry)
        return target.printers.setdefault(
            printer.id,
            {
                "printer_id": printer.id,
                "name": printer.name,
                "running_version": current.version if current is not None else None,
                "desired_version": target.candidate.version,
                "status": "current"
                if current is target.candidate.plugin
                and (manager.get_client(printer.id) is not None or not printer.is_active)
                else "applying",
                "reason": None,
                "error": None,
            },
        )

    def describe(self, plugin_id: str) -> dict | None:
        target = self._targets.get(plugin_id)
        if target is None:
            return None
        printers = [dict(row) for _, row in sorted(target.printers.items())]
        status = (
            "rolling_out"
            if any(row["status"] == "applying" for row in printers)
            else "error"
            if any(row["status"] == "error" for row in printers)
            else "current"
        )
        return {"status": status, "desired_version": target.candidate.version, "printers": printers}

    def queue_admission_blocked(self, printer_id: int | None) -> bool:
        # A refresh never drains the queue or waits for a physical print. The
        # gate exists only during the synchronous pin/client publication.
        return is_applying(printer_id)

    def dispatch_blocked(self, printer_id: int | None) -> bool:
        """Public admission check shared by direct starts and the scheduler."""
        return self.queue_admission_blocked(printer_id)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="printer-plugin-rollouts")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await self._restore()
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Printer plugin rollout sweep failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass

    async def _restore(self) -> None:
        """Resume persisted selections/pins when the host itself restarts."""
        from backend.app.printers.management import PluginManager

        _, _, _, registry = self._services()
        state = await asyncio.to_thread(PluginManager().read_state)
        for plugin_id, record in state["plugins"].items():
            if plugin_id in self._targets or not record.get("enabled", True):
                continue
            version = record.get("desired_version")
            if version is None:
                continue
            try:
                candidate = await asyncio.to_thread(registry.prepare, plugin_id, version)
                if registry.get(plugin_id) is candidate.plugin:
                    await self.request(plugin_id, candidate)
            except Exception:
                logger.exception("Unable to restore pending printer plugin rollout %s", plugin_id)

    async def _refresh_receipts(self, db, printer, old, captured_ids=()):
        """Read pending task outcomes on their old provider, without resending work."""
        from backend.app.models.printer_operation import PrinterOperation
        from backend.app.printers.management import printer_pin_identity
        from backend.app.printers.sdk import OperationDescriptor, OperationRequest, validate_receipt

        original_config = old._config
        identity = printer_pin_identity(original_config)
        if printer_pin_identity(PrinterConfig.from_printer(printer)) != identity:
            return "The original operation belongs to a different saved printer identity"
        pending = list(
            (
                await db.scalars(
                    select(PrinterOperation).where(
                        PrinterOperation.printer_id == printer.id,
                        PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")),
                    )
                )
            ).all()
        )
        _, _, _, registry = self._services()
        version_key = registry.runtime_for(old._plugin).version
        for operation in pending:
            binding = operation.operation_schema.get("extensions", {}).get("bambuddy:provider")
            if binding is not None:
                if binding.get("plugin_id") != old._plugin.id or binding.get("version") != version_key:
                    continue
                if binding.get("identity") != identity:
                    return f"SDK operation {operation.id} belongs to a different printer identity"
            elif operation.id not in captured_ids:
                continue
            if old is None or not operation.provider_task_id:
                return f"SDK operation {operation.id} is awaiting a known outcome"
            try:
                descriptor = OperationDescriptor.model_validate(operation.operation_schema)
                request = OperationRequest.model_validate(operation.request_data)

                async def read_status(task_id=operation.provider_task_id):
                    provider = old._plugin
                    args = (original_config, old._connection, task_id)
                    if inspect.iscoroutinefunction(provider.operation_status):
                        return await provider.operation_status(*args)
                    value = await asyncio.to_thread(provider.operation_status, *args)
                    return await value if inspect.isawaitable(value) else value

                receipt = validate_receipt(
                    descriptor,
                    request,
                    await asyncio.wait_for(read_status(), timeout=min(5, descriptor.timeout_seconds)),
                )
                await db.execute(
                    update(PrinterOperation)
                    .where(
                        PrinterOperation.id == operation.id,
                        PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")),
                    )
                    .values(receipt=receipt.model_dump(mode="json"))
                )
                await db.commit()
                if receipt.status not in {"accepted", "running"}:
                    registry.release_runtime("operation:" + operation.id)
                if receipt.status in {"accepted", "running"}:
                    return f"SDK operation {operation.id} is active"
            except Exception:
                # An unavailable observer or a timeout is not evidence that a
                # hardware operation ended. Preserve the durable receipt.
                logger.warning("Cannot confirm printer %s operation %s ended", printer.id, operation.id, exc_info=True)
                return f"SDK operation {operation.id} cannot yet be confirmed complete"
        return None

    async def _capture_existing_work(self, db, printer):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_operation import PrinterOperation

        queue_items = list(
            (
                await db.scalars(
                    select(PrintQueueItem).where(
                        PrintQueueItem.printer_id == printer.id,
                        or_(
                            PrintQueueItem.status.in_(("printing", "uploading", "starting", "paused")),
                            PrintQueueItem.dispatching_at.is_not(None),
                        ),
                    )
                )
            ).all()
        )
        receipts = list(
            (
                await db.scalars(
                    select(PrinterOperation.id).where(
                        PrinterOperation.printer_id == printer.id,
                        PrinterOperation.receipt["status"].as_string().in_(("accepted", "running")),
                    )
                )
            ).all()
        )
        _, _, scheduler, _ = self._services()
        return {
            "queue_ids": [item.id for item in queue_items],
            "queue_job_ids": {
                item.sdk_job["job_id"]
                for item in queue_items
                if isinstance(item.sdk_job, dict)
                and isinstance(item.sdk_job.get("job_id"), str)
                and item.sdk_job["job_id"].strip()
            },
            "receipt_ids": receipts,
            "inflight_tasks": [task for task, pid in scheduler._inflight.values() if pid == printer.id],
            "dispatch_token": scheduler._dispatch_holds.get(printer.id),
        }

    async def _apply(self, target, row, printer, db, manager, scheduler, registry):
        printer_id = printer.id
        old = manager.get_client(printer_id)
        current = self._provider(printer, manager, registry)
        if current is not None and current is target.candidate.plugin and (old is not None or not printer.is_active):
            row.update(status="current", running_version=current.version, reason=None, error=None)
            return
        if row["status"] == "error":
            return
        prepared = None
        published = False
        gated = False
        try:
            row.update(status="applying", reason="Refreshing the data observer", error=None)
            if not printer.is_active and old is None:
                registry.pin_printer(PrinterConfig.from_printer(printer), target.candidate.plugin)
                row.update(status="current", running_version=target.candidate.plugin.version, reason=None)
                return
            prepared = manager.prepare_connection(printer, target.candidate.plugin)
            if not prepared.reused or not prepared.client.state.connected:
                prepared.client.connect()
            deadline = asyncio.get_running_loop().time() + self.connection_timeout
            while not prepared.client.state.connected:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("The replacement provider did not establish a fresh connection")
                await asyncio.sleep(0.1)
            if self._targets.get(printer.plugin_id) is not target:
                return
            if manager.get_client(printer_id) is not old:
                raise PrinterPluginError("The printer connection changed during its refresh")
            from backend.app.models.printer import Printer

            factory, _, _, _ = self._services()
            async with factory() as final_db:
                fresh = await final_db.get(Printer, printer_id)
                if fresh is None or PrinterConfig.from_printer(fresh) != prepared.config:
                    raise PrinterPluginError("Printer configuration changed during its refresh")
                previous_work = await self._capture_existing_work(final_db, fresh)
            previous_job = manager._job_identity(old) if old is not None else None
            # There is intentionally no physical-state, queue-idleness or
            # provider-readiness condition here. Accepted work remains bound
            # to its original connection after the new display is published.
            if not _begin_cutover(printer_id):
                raise PrinterUpdateInProgress("Another observer publication is in progress")
            gated = True
            manager.publish_connection(
                prepared,
                retain_old=old is not None,
                workflow_job_id=previous_job,
                workflow_job_ids=previous_work.pop("queue_job_ids", set()),
            )
            published = True
            if old is not None:
                manager.annotate_retained(printer_id, old._plugin, previous_work)
            row.update(status="current", running_version=target.candidate.plugin.version, reason=None, error=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Printer %s observer refresh failed; retaining its current provider", printer_id)
            row.update(status="error", reason=None, error=f"{type(exc).__name__}: replacement provider unavailable")
        finally:
            if gated:
                _end_cutover(printer_id)
            if prepared is not None and not published and not prepared.reused:
                try:
                    await asyncio.to_thread(prepared.client.disconnect, timeout=1)
                except Exception:
                    logger.warning("Failed to close unpublished printer observer %s", printer_id, exc_info=True)

    async def _retire_completed_observers(self, db, manager, registry, printers):
        configured = {printer.id: printer for printer in printers}
        factory, _, scheduler, _ = self._services()
        semaphore = asyncio.Semaphore(self.max_parallel)

        async def retire(printer_id, record):
            printer = configured.get(printer_id)
            if printer is None:
                return
            async with semaphore:
                try:
                    async with factory() as own_db:
                        await self._retire_observer(own_db, manager, registry, scheduler, printer, printer_id, record)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Retaining previous printer observer %s until its work can be checked",
                        printer_id,
                        exc_info=True,
                    )

        await asyncio.gather(*(retire(printer_id, record) for printer_id, record in manager.retained_connections()))

    async def _retire_observer(self, db, manager, registry, scheduler, printer, printer_id, record):
        from backend.app.models.print_queue import PrintQueueItem

        old = record["client"]
        if printer is None:
            return
        if _leases.get(printer_id) or manager.provider_callbacks_pending(printer_id, old._plugin):
            return
        if any(not task.done() for task in record.get("inflight_tasks", [])):
            return
        dispatch_token = record.get("dispatch_token")
        if dispatch_token is not None and scheduler._dispatch_holds.get(printer_id) == dispatch_token:
            if scheduler._printer_in_dispatch_hold(printer_id):
                return
        if not await old.poll_pending_operations():
            return
        original_jobs = record.get("job_ids", set())
        state = old.state
        if manager._job_identity(old) in original_jobs:
            if not state.connected or state.state in manager.ACTIVE_PRINT_STATES:
                return
        queue_ids = record.get("queue_ids", [])
        if (
            queue_ids
            and await db.scalar(
                select(PrintQueueItem.id)
                .where(
                    PrintQueueItem.id.in_(queue_ids),
                    or_(
                        PrintQueueItem.status.in_(("printing", "uploading", "starting", "paused")),
                        PrintQueueItem.dispatching_at.is_not(None),
                    ),
                )
                .limit(1)
            )
            is not None
        ):
            return
        if await self._refresh_receipts(db, printer, old, record.get("receipt_ids", [])) is not None:
            return
        if not original_jobs:
            ready = old._plugin.can_deactivate(old._config, old._connection)
            if inspect.isawaitable(ready):
                ready = await asyncio.wait_for(ready, timeout=5)
            if ready is not True:
                return
        await manager.retire_connection(printer_id, old._plugin)

    async def _prune_pins(self, factory, registry) -> None:
        from backend.app.models.printer import Printer
        from backend.app.printers.management import PluginManager, printer_pin_identity

        pins = await asyncio.to_thread(PluginManager().get_printer_pins)
        if not pins:
            return
        async with factory() as db:
            printers = list((await db.scalars(select(Printer))).all())
        configured = {printer.id: PrinterConfig.from_printer(printer) for printer in printers}
        for printer_id, pin in pins.items():
            config = configured.get(int(printer_id))
            if (
                config is None
                or config.plugin_id != pin["plugin_id"]
                or printer_pin_identity(config) != pin["identity"]
            ):
                # Compare-and-swap prevents deleting a newer pin saved after
                # this database/configuration snapshot was read.
                registry.release_printer(int(printer_id), forget=True, expected_pin=pin)

    async def run_once(self) -> None:
        from backend.app.models.printer import Printer

        if self._run_lock.locked():
            return
        async with self._run_lock:
            factory, manager, scheduler, registry = self._services()
            await self._prune_pins(factory, registry)
            semaphore = asyncio.Semaphore(self.max_parallel)

            async def sweep(plugin_id, target):
                async with factory() as db:
                    printers = list((await db.scalars(select(Printer).where(Printer.plugin_id == plugin_id))).all())
                ids = {printer.id for printer in printers}
                for removed in set(target.printers) - ids:
                    del target.printers[removed]

                async def refresh(printer):
                    async with semaphore:
                        row = self._row(target, printer, manager, registry)
                        await self._apply(target, row, printer, None, manager, scheduler, registry)

                await asyncio.gather(*(refresh(printer) for printer in printers))
                async with factory() as db:
                    await self._retire_completed_observers(db, manager, registry, printers)
                from backend.app.printers.lifecycle import lifecycle

                await lifecycle.reconcile(plugin_id)

            await asyncio.gather(*(sweep(plugin_id, target) for plugin_id, target in list(self._targets.items())))


rollouts = RolloutCoordinator()


def queue_admission_blocked(printer_id: int | None) -> bool:
    return rollouts.queue_admission_blocked(printer_id)

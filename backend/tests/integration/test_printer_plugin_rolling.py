"""Rolling updates move an idle observer while retaining busy pinned versions."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.printers.contracts import PrinterPlugin
from backend.app.printers.registry import PrinterPluginRegistry, RuntimeProvider
from backend.app.printers.rolling import PrinterUpdateInProgress, RolloutCoordinator, operation_lease
from backend.app.printers.sdk import OperationDescriptor, OperationReceipt, OperationRequest
from backend.app.printers.sdk.host import HostPrinterState
from backend.app.services.printer_manager import PrinterManager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class FakeConnection:
    def __init__(self, owner, config, callbacks):
        self.owner = owner
        self.printer_id = config.printer_id
        self.callbacks = callbacks
        self.state = HostPrinterState()
        self.disconnected = 0
        self.connected_count = 0

    def connect(self, loop=None):
        self.connected_count += 1
        self.state.connected = self.owner.connect_ok
        self.state.state = "IDLE"
        if self.owner.connect_callback is not None:
            self.owner.connect_callback(self)
        self.callbacks["on_state_change"](self.state)

    def disconnect(self, timeout=0):
        self.disconnected += 1
        self.state.connected = False

    def check_staleness(self):
        return self.state.connected


class FakePlugin(PrinterPlugin):
    id = "rollingexample"
    name = "Rolling fixture"

    def __init__(self, version):
        self.version = version
        self.connect_ok = True
        self.connect_callback = None
        self.ready = True
        self.connections = []
        self.polled = []
        self.receipt = None

    def create_connection(self, config, **callbacks):
        connection = FakeConnection(self, config, callbacks)
        self.connections.append(connection)
        return connection

    def can_deactivate(self, config, connection):
        return self.ready

    async def operation_status(self, config, connection, task_id):
        self.polled.append(task_id)
        if isinstance(self.receipt, Exception):
            raise self.receipt
        return self.receipt


@pytest.fixture
def rolling_case(test_engine, tmp_path, monkeypatch):
    from backend.app.core.config import settings
    from backend.app.printers import registry as registry_module, rolling
    from backend.app.printers.lifecycle import lifecycle
    from backend.app.services import print_scheduler as scheduler_module
    from backend.app.services.print_scheduler import PrintScheduler

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "printer_plugin_dir", tmp_path / "plugins")
    registry = PrinterPluginRegistry()
    registry._loaded = True
    old, new = FakePlugin("1.0.0"), FakePlugin("2.0.0")
    registry.register(old)
    monkeypatch.setattr(registry_module, "registry", registry)
    manager = PrinterManager()
    scheduler = PrintScheduler()
    monkeypatch.setattr(scheduler_module, "printer_manager", manager)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    coordinator = RolloutCoordinator(session_factory=factory, manager=manager, scheduler=scheduler, registry=registry)
    coordinator.connection_timeout = 0.01
    monkeypatch.setattr(rolling, "rollouts", coordinator)
    monkeypatch.setattr(lifecycle, "reconcile", AsyncMock())
    candidate = RuntimeProvider(new, "2.0.0", {"version": "2.0.0", "source": "managed-package"})

    def attach(printer, state="IDLE"):
        prepared = manager.prepare_connection(printer, old)
        manager.publish_connection(prepared)
        prepared.client.connect()
        prepared.client.state.state = state
        if state in manager.ACTIVE_PRINT_STATES:
            prepared.client.state.subtask_id = f"native-{printer.id}"
        return prepared.client

    async def request():
        registry.activate(candidate)
        await coordinator.request(old.id, candidate)

    return SimpleNamespace(
        registry=registry,
        manager=manager,
        scheduler=scheduler,
        coordinator=coordinator,
        old=old,
        new=new,
        candidate=candidate,
        attach=attach,
        request=request,
    )


async def test_busy_printer_observer_refreshes_immediately_and_retains_original_work(rolling_case, printer_factory):
    case = rolling_case
    busy = await printer_factory(plugin_id=case.old.id, name="Busy printer")
    idle = await printer_factory(plugin_id=case.old.id, name="Idle printer")
    busy_client, idle_client = case.attach(busy, "PAUSE"), case.attach(idle)

    def native_state(connection):
        if connection.printer_id == busy.id:
            connection.state.state = "PAUSE"
            connection.state.subtask_id = f"native-{busy.id}"

    case.new.connect_callback = native_state
    await case.request()
    assert not case.coordinator.dispatch_blocked(busy.id)
    await case.coordinator.run_once()
    assert case.manager.get_client(busy.id)._plugin is case.new
    assert case.manager.get_client(busy.id).state.state == "PAUSE"
    assert case.manager.retained_connection(busy.id, case.old) is busy_client
    assert busy_client._connection.disconnected == 0 and case.registry.provider_for(busy) is case.new
    assert case.manager.get_client(idle.id)._plugin is case.new
    assert idle_client._connection.disconnected == 1 and case.registry.provider_for(idle) is case.new
    rows = {row["printer_id"]: row for row in case.coordinator.describe(case.old.id)["printers"]}
    assert rows[busy.id]["status"] == "current" and rows[idle.id]["status"] == "current"
    assert not case.coordinator.dispatch_blocked(idle.id)
    busy_client.state.state = "IDLE"
    await case.coordinator.run_once()
    assert case.manager.get_client(busy.id)._plugin is case.new
    assert busy_client._connection.disconnected == 1
    assert case.coordinator.describe(case.old.id)["status"] == "current"


async def test_core_queue_receipt_retains_original_adapter_before_running_telemetry(
    rolling_case, printer_factory, monkeypatch
):
    from backend.app.printers.job_dispatch import start_queue_job
    from backend.app.printers.sdk import DeviceDescriptor, JobRequest, ResourceDescriptor

    case = rolling_case
    case.old.api_version = case.new.api_version = 2

    def describe(config):
        return DeviceDescriptor(
            device_id=config.serial_number,
            resources=[ResourceDescriptor(id="tool:opaque", kind="tool", label="Physical tool")],
            operations=[
                OperationDescriptor(
                    id="job.start",
                    label="Start job",
                    asynchronous=True,
                    parameters_schema=JobRequest.model_json_schema(),
                )
            ],
        )

    monkeypatch.setattr(case.old, "describe_device", describe)
    monkeypatch.setattr(case.new, "describe_device", describe)
    printer = await printer_factory(plugin_id=case.old.id)
    original = case.attach(printer, "IDLE")
    native = original._connection
    sent = []

    async def invoke(config, connection, request):
        assert connection is native
        sent.append(request)
        case.old.receipt = OperationReceipt(
            operation_id=request.operation_id,
            request_id=request.request_id,
            status="accepted",
            task_id="native-pending-start",
        )
        return case.old.receipt

    monkeypatch.setattr(case.old, "invoke", invoke)
    item = SimpleNamespace(
        id=9001, sdk_job=None, tool_ids=["tool:opaque"], material_bindings=None, job_options={}, plate_id=None
    )
    assert await start_queue_job(item, printer, original, "uploaded.gcode")
    assert native.state.state == "IDLE"  # Hardware has not published RUNNING yet.
    assert "native-pending-start" in original._active_tasks
    assert len(sent) == 1
    await case.request()
    await case.coordinator.run_once()
    assert case.manager.get_client(printer.id)._plugin is case.new
    assert case.manager.retained_connection(printer.id, case.old) is original
    assert native.disconnected == 0
    await original.poll_pending_operations()
    assert case.old.polled and set(case.old.polled) == {"native-pending-start"}
    assert case.new.polled == [] and len(sent) == 1


@pytest.mark.parametrize("blocker", ["upload", "dispatch", "queue", "provider", "lease"])
async def test_existing_work_retains_its_handler_without_delaying_new_observer(
    rolling_case, printer_factory, db_session, blocker
):
    from backend.app.models.print_queue import PrintQueueItem

    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    client = case.attach(printer)
    await case.request()
    if blocker == "upload":
        case.scheduler._inflight[123] = (SimpleNamespace(done=lambda: False), printer.id)
    elif blocker == "dispatch":
        case.scheduler._dispatch_holds[printer.id] = (time.monotonic(), "IDLE", None)
    elif blocker == "queue":
        db_session.add(PrintQueueItem(printer_id=printer.id, status="printing"))
        await db_session.commit()
    elif blocker == "provider":
        case.old.ready = False
    if blocker == "lease":
        with operation_lease(printer.id):
            await case.coordinator.run_once()
    else:
        await case.coordinator.run_once()
    assert case.manager.get_client(printer.id)._plugin is case.new
    assert case.manager.retained_connection(printer.id, case.old) is client
    assert client._connection.disconnected == 0 and len(case.new.connections) == 1
    assert case.coordinator.describe(case.old.id)["printers"][0]["status"] == "current"


async def test_failed_replacement_keeps_old_connection_pin_and_queue_available(rolling_case, printer_factory):
    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    old = case.attach(printer)
    case.new.connect_ok = False
    await case.request()
    await case.coordinator.run_once()
    assert case.manager.get_client(printer.id) is old and case.registry.provider_for(printer) is case.old
    assert old._connection.disconnected == 0 and case.new.connections[0].disconnected == 1
    assert case.coordinator.describe(case.old.id)["status"] == "error"
    assert not case.coordinator.dispatch_blocked(printer.id)


async def test_printer_becoming_busy_during_handshake_still_publishes_the_new_observer(rolling_case, printer_factory):
    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    old = case.attach(printer)

    def started(candidate):
        old.state.state = candidate.state.state = "RUNNING"
        old.state.subtask_id = candidate.state.subtask_id = "native-current-job"

    case.new.connect_callback = started
    await case.request()
    await case.coordinator.run_once()
    assert case.manager.get_client(printer.id)._plugin is case.new and old._connection.disconnected == 0
    assert case.new.connections[0].disconnected == 0
    assert case.coordinator.describe(case.old.id)["printers"][0]["status"] == "current"


async def test_candidate_callbacks_are_quarantined_and_old_callbacks_retire_after_publication(
    rolling_case, printer_factory, monkeypatch
):
    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    old = case.attach(printer)
    delivered = []

    async def callback(printer_id, data):
        delivered.append(data)

    def execute(coroutine):
        # Record synchronously inside this test without scheduling host work.
        try:
            coroutine.send(None)
        except StopIteration:
            pass

    case.manager.set_print_start_callback(callback)
    monkeypatch.setattr(case.manager, "_schedule_async", execute)
    prepared = case.manager.prepare_connection(printer, case.new)
    prepared.client._connection.callbacks["on_print_start"]({"source": "candidate"})
    assert delivered == []
    case.registry.activate(case.candidate)
    case.manager.publish_connection(prepared)
    old._connection.callbacks["on_print_start"]({"source": "old"})
    prepared.client._connection.callbacks["on_print_start"]({"source": "new"})
    assert delivered == [{"source": "new"}]


@pytest.mark.parametrize("terminal", [False, True])
async def test_abandoned_receipt_is_polled_on_old_provider_without_replaying_command(
    rolling_case, printer_factory, db_session, terminal
):
    from backend.app.models.printer_operation import PrinterOperation

    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    old = case.attach(printer)
    request = OperationRequest(operation_id="example.calibrate")
    descriptor = OperationDescriptor(id=request.operation_id, label="Calibrate", asynchronous=True)
    row = PrinterOperation(
        id=str(uuid4()),
        printer_id=printer.id,
        operation_id=request.operation_id,
        idempotency_key="calibration",
        request_digest="0" * 64,
        request_data=request.model_dump(mode="json"),
        operation_schema=descriptor.model_dump(mode="json"),
        receipt={"status": "running"},
        provider_task_id="old-task",
        process_id=str(uuid4()),
    )
    db_session.add(row)
    await db_session.commit()
    case.old.receipt = OperationReceipt(
        operation_id=request.operation_id,
        request_id=request.request_id,
        status="succeeded" if terminal else "running",
        task_id="old-task",
        result=True,
    )
    await case.request()
    await case.coordinator.run_once()
    assert case.old.polled == ["old-task"] and case.new.polled == []
    await db_session.refresh(row)
    assert row.receipt["status"] == ("succeeded" if terminal else "running")
    assert case.manager.get_client(printer.id)._plugin is case.new
    assert (case.manager.retained_connection(printer.id, case.old) is old) is not terminal


async def test_only_atomic_publication_gate_blocks_new_work_and_refresh_does_not_drain_queue(
    rolling_case, printer_factory
):
    from backend.app.printers.rolling import _begin_cutover, _end_cutover

    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    case.attach(printer)
    assert _begin_cutover(printer.id)
    try:
        with pytest.raises(PrinterUpdateInProgress), operation_lease(printer.id):
            pass
    finally:
        _end_cutover(printer.id)
    await case.request()
    assert case.scheduler._is_printer_idle(printer.id)
    assert not case.coordinator.dispatch_blocked(printer.id)
    assert case.scheduler._inflight == {}


async def test_job_scoped_callbacks_complete_old_job_once_and_new_jobs_use_new_provider(
    rolling_case, printer_factory, monkeypatch
):
    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    old = case.attach(printer, "RUNNING")
    original = old.state.subtask_id
    delivered = []

    async def lifecycle(event, printer_id, data):
        provider = case.registry.provider_for(printer)
        connection = case.manager.get_client(printer_id)
        delivered.append((event, data["job_id"], provider.version, connection._plugin.version))

    async def started(printer_id, data):
        await lifecycle("start", printer_id, data)

    async def completed(printer_id, data):
        await lifecycle("complete", printer_id, data)

    async def layer(printer_id, value):
        connection = case.manager.get_client(printer_id)
        delivered.append(("layer", connection.state.subtask_id, value, connection._plugin.version))

    def execute(coroutine):
        try:
            coroutine.send(None)
        except StopIteration:
            pass

    monkeypatch.setattr(case.manager, "_schedule_async", execute)
    case.manager.set_print_start_callback(started)
    case.manager.set_print_complete_callback(completed)
    case.manager.set_layer_change_callback(layer)
    old._connection.callbacks["on_print_start"]({"job_id": original})

    def same_print(candidate):
        candidate.state.state = "RUNNING"
        candidate.state.subtask_id = original

    case.new.connect_callback = same_print
    await case.request()
    await case.coordinator.run_once()
    new = case.manager.get_client(printer.id)
    assert new._plugin is case.new
    new._connection.callbacks["on_print_start"]({"job_id": original})
    old._connection.callbacks["on_layer_change"](3)
    new._connection.callbacks["on_layer_change"](3)
    new._connection.callbacks["on_print_complete"]({"job_id": original})
    old.state.state = "FINISH"
    old._connection.callbacks["on_print_complete"]({"job_id": original})
    old._connection.callbacks["on_print_complete"]({"job_id": original})
    await case.coordinator.run_once()
    assert case.manager.retained_connection(printer.id, case.old) is None
    new._connection.callbacks["on_print_complete"]({"job_id": original})

    old.state.state = new.state.state = "RUNNING"
    old.state.subtask_id = new.state.subtask_id = "next-native-job"
    old._connection.callbacks["on_print_start"]({"job_id": "next-native-job"})
    new._connection.callbacks["on_print_start"]({"job_id": "next-native-job"})
    old._connection.callbacks["on_layer_change"](4)
    new._connection.callbacks["on_layer_change"](4)
    new._connection.callbacks["on_print_complete"]({"job_id": "next-native-job"})
    assert delivered == [
        ("start", original, "1.0.0", "1.0.0"),
        ("layer", original, 3, "1.0.0"),
        ("complete", original, "1.0.0", "1.0.0"),
        ("start", "next-native-job", "2.0.0", "2.0.0"),
        ("layer", "next-native-job", 4, "2.0.0"),
        ("complete", "next-native-job", "2.0.0", "2.0.0"),
    ]


async def test_offline_peer_does_not_delay_connected_printer_observer(rolling_case, printer_factory):
    case = rolling_case
    offline = await printer_factory(plugin_id=case.old.id, name="Offline")
    online = await printer_factory(plugin_id=case.old.id, name="Online")
    case.attach(offline)
    case.attach(online, "PAUSE")
    started = asyncio.Event()
    case.coordinator.connection_timeout = 1

    def candidate_state(connection):
        if connection.printer_id == offline.id:
            connection.state.connected = False
            started.set()

    case.new.connect_callback = candidate_state
    await case.request()
    task = asyncio.create_task(case.coordinator.run_once())
    await started.wait()
    for _ in range(100):
        if case.manager.get_client(online.id)._plugin is case.new:
            break
        await asyncio.sleep(0.01)
    assert case.manager.get_client(online.id)._plugin is case.new
    assert not task.done()
    assert not case.coordinator.dispatch_blocked(offline.id)
    with operation_lease(offline.id):
        pass
    case.new.connections[0].state.connected = True
    await task


async def test_unknown_task_status_keeps_original_handler_without_blocking_refresh(
    rolling_case, printer_factory, db_session
):
    from backend.app.models.printer_operation import PrinterOperation

    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    old = case.attach(printer)
    request = OperationRequest(operation_id="example.calibrate")
    operation = OperationDescriptor(id=request.operation_id, label="Calibrate", asynchronous=True)
    row = PrinterOperation(
        id=str(uuid4()),
        printer_id=printer.id,
        operation_id=request.operation_id,
        idempotency_key="unknown",
        request_digest="0" * 64,
        request_data=request.model_dump(mode="json"),
        operation_schema=operation.model_dump(mode="json"),
        receipt={"status": "accepted"},
        provider_task_id="uncertain-task",
        process_id=str(uuid4()),
    )
    db_session.add(row)
    await db_session.commit()
    case.old.receipt = RuntimeError("Status observer is unavailable")
    await case.request()
    await case.coordinator.run_once()
    assert case.manager.get_client(printer.id)._plugin is case.new
    assert case.manager.retained_connection(printer.id, case.old) is old
    await db_session.refresh(row)
    assert row.receipt == {"status": "accepted"}
    assert not case.coordinator.dispatch_blocked(printer.id)


async def test_pin_pruning_forgets_only_deleted_or_reassigned_device_records(rolling_case, printer_factory):
    from dataclasses import replace

    from backend.app.printers.contracts import PrinterConfig
    from backend.app.printers.management import PluginManager

    case = rolling_case
    current = await printer_factory(plugin_id=case.old.id)
    reassigned = await printer_factory(plugin_id=case.old.id, serial_number="NEW-DEVICE")
    case.registry.pin_printer(current, case.old)
    case.registry.pin_printer(replace(PrinterConfig.from_printer(reassigned), serial_number="OLD-DEVICE"), case.old)
    case.registry.pin_printer(replace(PrinterConfig.from_printer(current), printer_id=999), case.old)
    await case.coordinator.run_once()
    assert set(PluginManager().get_printer_pins()) == {str(current.id)}
    assert reassigned.id not in case.registry._pins and 999 not in case.registry._pins


async def test_pin_pruning_preserves_concurrent_valid_replacement(rolling_case, printer_factory, monkeypatch):
    from dataclasses import replace

    from backend.app.printers.contracts import PrinterConfig
    from backend.app.printers.management import PluginManager

    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id, serial_number="NEW-DEVICE")
    config = PrinterConfig.from_printer(printer)
    case.registry.pin_printer(replace(config, serial_number="OLD-DEVICE"), case.old)
    case.registry.activate(case.candidate)
    original = PluginManager.remove_printer_pin

    def replace_before_delete(manager, printer_id, *, expected=None):
        manager.set_printer_pin(config, "2.0.0")
        return original(manager, printer_id, expected=expected)

    monkeypatch.setattr(PluginManager, "remove_printer_pin", replace_before_delete)
    await case.coordinator.run_once()
    assert PluginManager().get_printer_pins()[str(printer.id)]["version"] == "2.0.0"
    assert case.registry.provider_for(printer) is case.new


async def test_receipt_identity_mismatch_is_not_polled_against_original_connection(
    rolling_case, printer_factory, db_session
):
    from backend.app.models.printer_operation import PrinterOperation

    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    old = case.attach(printer)
    request = OperationRequest(operation_id="example.calibrate")
    descriptor = OperationDescriptor(id=request.operation_id, label="Calibrate", asynchronous=True)
    schema = descriptor.model_dump(mode="json")
    schema["extensions"] = {
        "bambuddy:provider": {
            "plugin_id": case.old.id,
            "version": "1.0.0",
            "identity": "0" * 64,
        }
    }
    row = PrinterOperation(
        id=str(uuid4()),
        printer_id=printer.id,
        operation_id=request.operation_id,
        idempotency_key="wrong-device",
        request_digest="0" * 64,
        request_data=request.model_dump(mode="json"),
        operation_schema=schema,
        receipt={"status": "running"},
        provider_task_id="foreign-task",
        process_id=str(uuid4()),
    )
    db_session.add(row)
    await db_session.commit()
    await case.request()
    await case.coordinator.run_once()
    assert case.manager.get_client(printer.id)._plugin is case.new
    assert case.manager.retained_connection(printer.id, case.old) is old
    assert case.old.polled == []
    await db_session.refresh(row)
    assert row.receipt == {"status": "running"}


async def test_repeated_rollback_revives_exact_running_observer_and_accepted_task(
    rolling_case, printer_factory, monkeypatch
):
    from backend.app.printers.operation_versions import operation_connection
    from backend.app.services import printer_manager as manager_module

    case = rolling_case
    monkeypatch.setattr(manager_module, "printer_manager", case.manager)
    printer = await printer_factory(plugin_id=case.old.id)
    original = case.attach(printer, "RUNNING")
    original_job = original.state.subtask_id
    request = OperationRequest(operation_id="example.calibrate")
    descriptor = OperationDescriptor(id=request.operation_id, label="Calibrate", asynchronous=True)
    running = OperationReceipt(
        operation_id=request.operation_id, request_id=request.request_id, status="running", task_id="original-task"
    )
    original._connection.task_receipts = {"original-task": running}

    async def original_status(config, connection, task_id):
        return connection.task_receipts[task_id]

    case.old.operation_status = original_status
    original.track_operation(descriptor, request, running)

    def same_job(connection):
        connection.state.state = "RUNNING"
        connection.state.subtask_id = original_job

    case.new.connect_callback = same_job
    first = case.registry.runtime_for(case.old)
    for index, selected in enumerate((case.candidate, first, case.candidate, first)):
        case.registry.activate(selected)
        await case.coordinator.request(case.old.id, selected)
        await case.coordinator.run_once()
        if index == 0:
            # Both versions have accepted work, so each repeated rollback must
            # revive its exact retained observer rather than a fresh instance.
            second = case.manager.get_client(printer.id)
            second_request = OperationRequest(operation_id="example.calibrate")
            second_receipt = OperationReceipt(
                operation_id=second_request.operation_id,
                request_id=second_request.request_id,
                status="running",
                task_id="second-task",
            )
            second._connection.task_receipts = {"second-task": second_receipt}
            case.new.operation_status = original_status
            second.track_operation(descriptor, second_request, second_receipt)
        assert case.manager.retained_connection(printer.id, case.old) is original
        assert operation_connection(printer.id, case.old) is original._connection
        receipt = await case.old.operation_status(
            original._config, operation_connection(printer.id, case.old), "original-task"
        )
        assert receipt.status == "running"
        assert len(case.old.connections) == len(case.new.connections) == 1
        assert case.old.connections[0].connected_count == case.new.connections[0].connected_count == 1
        refs = [key for key in case.registry._runtime_refs if key.startswith("printer-observer:")]
        assert len(refs) == 1
    assert case.manager.get_client(printer.id) is original
    assert (printer.id, id(case.old)) not in case.manager._retained_clients
    assert original._connection.disconnected == 0


@pytest.mark.parametrize("source", ["accepted_task", "queue_plan"])
async def test_accepted_job_before_first_telemetry_keeps_original_lifecycle_owner(
    rolling_case, printer_factory, db_session, monkeypatch, source
):
    from backend.app.models.print_queue import PrintQueueItem

    case = rolling_case
    printer = await printer_factory(plugin_id=case.old.id)
    original = case.attach(printer)  # The accepted job has not reached telemetry yet.
    request = OperationRequest(operation_id="job.start", parameters={"job_id": "accepted-job-Y"})
    descriptor = OperationDescriptor(id="job.start", label="Print", asynchronous=True)
    running = OperationReceipt(
        operation_id="job.start", request_id=request.request_id, status="running", task_id="accepted-job-Y"
    )
    queue_item = None
    if source == "accepted_task":
        case.old.receipt = running
        original.track_operation(descriptor, request, running)
    else:
        queue_item = PrintQueueItem(printer_id=printer.id, status="printing", sdk_job={"job_id": "accepted-job-Y"})
        db_session.add(queue_item)
        await db_session.commit()
    events = []

    async def started(printer_id, data):
        events.append(("start", data["job_id"], case.registry.provider_for(printer).version))

    async def completed(printer_id, data):
        events.append(("complete", data["job_id"], case.registry.provider_for(printer).version))

    def execute(coroutine):
        try:
            coroutine.send(None)
        except StopIteration:
            pass

    monkeypatch.setattr(case.manager, "_schedule_async", execute)
    case.manager.set_print_start_callback(started)
    case.manager.set_print_complete_callback(completed)
    await case.request()
    await case.coordinator.run_once()
    display = case.manager.get_client(printer.id)
    original.state.state = display.state.state = "RUNNING"
    original.state.subtask_id = display.state.subtask_id = "accepted-job-Y"
    display._connection.callbacks["on_print_start"]({"job_id": "accepted-job-Y"})
    original._connection.callbacks["on_print_start"]({"job_id": "accepted-job-Y"})
    display._connection.callbacks["on_print_complete"]({"job_id": "accepted-job-Y"})
    original.state.state = "FINISH"
    original._connection.callbacks["on_print_complete"]({"job_id": "accepted-job-Y"})
    if queue_item is not None:
        queue_item.status = "completed"
        await db_session.commit()
    else:
        case.old.receipt = running.model_copy(update={"status": "succeeded", "result": True})
    await case.coordinator.run_once()
    display.state.state = "RUNNING"
    display.state.subtask_id = "future-job-Z"
    display._connection.callbacks["on_print_start"]({"job_id": "future-job-Z"})
    display._connection.callbacks["on_print_complete"]({"job_id": "future-job-Z"})
    assert events == [
        ("start", "accepted-job-Y", "1.0.0"),
        ("complete", "accepted-job-Y", "1.0.0"),
        ("start", "future-job-Z", "2.0.0"),
        ("complete", "future-job-Z", "2.0.0"),
    ]

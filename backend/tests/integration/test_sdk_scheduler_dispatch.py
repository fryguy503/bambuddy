"""Resource jobs pass the real scheduler without legacy vendor preparation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.printers.api import PrinterConnectionAdapter
from backend.app.printers.contracts import PrinterConfig, PrinterModel, PrinterPlugin
from backend.app.printers.registry import PrinterPluginRegistry
from backend.app.printers.sdk import (
    DeviceDescriptor,
    OperationDescriptor,
    OperationError,
    OperationReceipt,
    ResourceDescriptor,
)
from backend.app.printers.sdk.host import HostPrinterState
from backend.app.services import print_scheduler as scheduler_module
from backend.app.services.print_scheduler import PrintScheduler
from backend.tests._fixtures.background_tasks import close_and_default

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class ResourcePrinter(PrinterPlugin):
    id = "opaque_tools"
    name = "Opaque tools fixture"
    api_version = 2
    publisher_id = "tests"
    descriptor = DeviceDescriptor(
        device_id="fixture",
        resources=[
            ResourceDescriptor(id=f"tool:dock-{name}", kind="tool", label=name)
            for name in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")
        ],
        operations=[OperationDescriptor(id="job.start", label="Start job", parameters_schema={"type": "object"})],
    )
    models = (PrinterModel("future", "Future six-tool printer", device_template=descriptor),)

    def __init__(self):
        self.requests = []
        self.connection = SimpleNamespace(
            state=HostPrinterState(
                connected=True,
                state="IDLE",
                plugin_id=self.id,
                material_inventory=[
                    {"source_id": "feed:opaque", "tool_ids": ["tool:dock-foxtrot"], "material_type": "PLA"}
                ],
            )
        )

    def create_connection(self, config, **callbacks):
        return self.connection

    def operation_handlers(self, connection):
        def start(request):
            self.requests.append(request)
            return True

        return {"job.start": start}


@pytest.fixture
async def resource_case(db_session, printer_factory, archive_factory, tmp_path, monkeypatch):
    from backend.app.printers import registry as registry_module

    registry = PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    registry.load()
    plugin = ResourcePrinter()
    registry.register(plugin)
    printer = await printer_factory(plugin_id=plugin.id, model="future")
    source = tmp_path / "opaque.gcode"
    source.write_bytes(b"; non-Bambu artifact")
    archive = await archive_factory(
        printer.id, filename=source.name, file_path=str(source), nozzle_diameter="0.8", with_run=False
    )
    item = PrintQueueItem(
        printer_id=printer.id,
        archive_id=archive.id,
        status="pending",
        tool_ids=[resource.id for resource in plugin.descriptor.resources],
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    adapter = PrinterConnectionAdapter(PrinterConfig.from_printer(printer), plugin.connection)
    scheduler = PrintScheduler()
    manager = scheduler_module.printer_manager
    monkeypatch.setattr(manager, "is_connected", lambda printer_id: True)
    monkeypatch.setattr(manager, "get_client", lambda printer_id: adapter)
    monkeypatch.setattr(manager, "get_status", lambda printer_id: adapter.state)
    monkeypatch.setattr(manager, "set_awaiting_plate_clear", MagicMock())
    legacy_start = MagicMock(side_effect=AssertionError("legacy command path reached"))
    monkeypatch.setattr(manager, "start_print", legacy_start)
    upload = AsyncMock(return_value=True)
    monkeypatch.setattr(scheduler_module, "upload_file_async", upload)
    monkeypatch.setattr(scheduler_module, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 1.0)))
    monkeypatch.setattr(scheduler_module, "cache_3mf_download", MagicMock())
    monkeypatch.setattr(scheduler_module, "spawn_background_task", close_and_default)
    monkeypatch.setattr(
        scheduler_module,
        "_installed_nozzle_diameters",
        MagicMock(side_effect=AssertionError("legacy nozzle guard reached")),
    )
    monkeypatch.setattr(
        scheduler_module, "_effective_plate_id", MagicMock(side_effect=AssertionError("legacy plate parser reached"))
    )
    monkeypatch.setattr(
        scheduler_module,
        "extract_rack_plan_from_3mf",
        MagicMock(side_effect=AssertionError("legacy rack parser reached")),
    )
    monkeypatch.setattr(
        scheduler_module.kprofile_drift,
        "reapply_lost_kprofiles",
        AsyncMock(side_effect=AssertionError("legacy calibration reached")),
    )
    monkeypatch.setattr(scheduler_module.notification_service, "on_queue_job_started", AsyncMock())
    monkeypatch.setattr(scheduler_module.mqtt_relay, "on_queue_job_started", AsyncMock())
    monkeypatch.setattr(scheduler, "_propagate_owner_to_printer_manager", AsyncMock())
    monkeypatch.setattr(scheduler, "_preheat_and_soak", AsyncMock(return_value=True))
    budget = AsyncMock(wraps=scheduler_module.validate_print_budget)
    monkeypatch.setattr(scheduler_module, "validate_print_budget", budget)
    return SimpleNamespace(
        plugin=plugin,
        scheduler=scheduler,
        printer=printer,
        item=item,
        upload=upload,
        legacy_start=legacy_start,
        budget=budget,
    )


async def test_six_opaque_tools_dispatch_through_scheduler(db_session, resource_case):
    case = resource_case
    assert await case.scheduler._ensure_ams_mapping(db_session, case.printer.id, case.item) is None
    assert not await case.scheduler._block_on_unmatched_filament(db_session, case.item)
    assert not await case.scheduler._block_on_filament_deficit(db_session, case.item)
    assert (
        case.scheduler._build_loaded_filaments(case.plugin.connection.state)
        == case.plugin.connection.state.material_inventory
    )
    await case.scheduler._start_print(db_session, case.item)
    case.budget.assert_awaited_once()
    case.scheduler._preheat_and_soak.assert_awaited_once()
    case.upload.assert_awaited_once()
    case.legacy_start.assert_not_called()
    assert case.item.status == "printing"
    assert len(case.plugin.requests) == 1
    request = case.plugin.requests[0]
    assert request.operation_id == "job.start"
    assert request.parameters["selected_tool_ids"] == case.item.tool_ids
    assert request.parameters["artifact"]["format"] == "gcode"
    assert case.item.ams_mapping is None


async def test_invalid_resource_plan_fails_before_upload(db_session, resource_case):
    case = resource_case
    case.item.tool_ids = ["tool:not-installed"]
    await db_session.commit()
    await case.scheduler._start_print(db_session, case.item)
    case.budget.assert_awaited_once()
    case.upload.assert_not_awaited()
    case.legacy_start.assert_not_called()
    assert case.item.status == "failed"
    assert "Invalid resource job plan" in case.item.error_message
    assert case.plugin.requests == []


async def test_provider_plate_hold_defers_before_budget_upload_or_start_with_global_setting_off(
    db_session, resource_case
):
    case = resource_case
    case.plugin.connection.state.plate_clear_required = True
    assert case.scheduler._can_dispatch_to_printer(case.printer.id, require_plate_clear=False) is False
    assert (
        case.scheduler._pinned_hold_reason(case.printer.id, "Fixture", False)
        == "Waiting for plate confirmation: Fixture"
    )
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "pending"
    assert case.item.waiting_reason == "Waiting for plate confirmation"
    assert case.item.error_message is None
    case.budget.assert_not_awaited()
    case.upload.assert_not_awaited()
    assert case.plugin.requests == []


async def test_retained_workflow_completion_after_observer_cutover_stays_pending(
    db_session, resource_case, monkeypatch
):
    from backend.app.services.printer_manager import PrinterManager

    case = resource_case
    state = case.plugin.connection.state
    state.plate_clear_required = False
    state.requires_plate_clear = True
    manager = scheduler_module.printer_manager
    monkeypatch.setattr(manager, "_awaiting_plate_clear", set())
    monkeypatch.setattr(manager, "_loop", None)
    assert case.scheduler._can_dispatch_to_printer(case.printer.id, require_plate_clear=False) is True
    # The retained owner's normal completion method updates shared host state;
    # the current observer snapshot deliberately stays unchanged.
    PrinterManager.set_awaiting_plate_clear(manager, case.printer.id, True)
    assert state.plate_clear_required is False
    assert case.scheduler._can_dispatch_to_printer(case.printer.id, require_plate_clear=False) is False
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "pending" and case.item.error_message is None
    assert case.item.waiting_reason.startswith("Waiting for plate confirmation")
    case.budget.assert_not_awaited()
    case.upload.assert_not_awaited()
    assert case.plugin.requests == []


async def test_provider_hold_arriving_during_upload_stays_pending_without_start(db_session, resource_case):
    case = resource_case

    async def upload(*args, **kwargs):
        case.plugin.connection.state.plate_clear_required = True
        return True

    case.upload.side_effect = upload
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "pending"
    assert case.item.error_message is None
    assert case.item.waiting_reason.startswith("Waiting for plate confirmation")
    assert case.plugin.requests == []


async def test_provider_hold_detected_at_final_send_is_deferred_not_generic_failure(
    db_session, resource_case, monkeypatch
):
    from backend.app.printers import job_dispatch

    case = resource_case

    async def refuse(*args, **kwargs):
        case.plugin.connection.state.plate_clear_required = True
        return False

    monkeypatch.setattr(job_dispatch, "start_queue_job", refuse)
    monkeypatch.setattr(scheduler_module, "delete_file_async", AsyncMock(return_value=True))
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "pending"
    assert case.item.error_message is None
    assert case.item.waiting_reason.startswith("Waiting for plate confirmation")
    assert case.plugin.requests == []


async def test_core_scheduler_preserves_validated_public_sdk_rejection(db_session, resource_case, monkeypatch):
    case = resource_case
    message = "The selected ACE material does not match the sliced filament type"

    def reject(request):
        case.plugin.requests.append(request)
        return OperationReceipt(
            operation_id=request.operation_id,
            request_id=request.request_id,
            status="failed",
            error=OperationError(
                code="fixture.material_mismatch", message=message, details={"private_token": "PRIVATE-DETAILS"}
            ),
        )

    monkeypatch.setattr(case.plugin, "operation_handlers", lambda connection: {"job.start": reject})
    monkeypatch.setattr(scheduler_module, "delete_file_async", AsyncMock(return_value=True))
    notify = AsyncMock()
    failed_toast = AsyncMock()
    monkeypatch.setattr(scheduler_module.notification_service, "on_queue_job_failed", notify)
    monkeypatch.setattr(scheduler_module.ws_manager, "send_queue_item_failed", failed_toast)
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "failed"
    assert case.item.error_message == message
    assert "PRIVATE-DETAILS" not in case.item.error_message
    assert notify.call_args.kwargs["reason"] == message
    assert failed_toast.call_args.kwargs["error_message"] == message
    assert "PRIVATE-DETAILS" not in str(failed_toast.call_args.kwargs)
    assert len(case.plugin.requests) == 1
    case.legacy_start.assert_not_called()


def material_issue(recovery="material_selection"):
    return {
        "code": "fixture.material_empty",
        "message": "Choose a loaded source",
        "recovery": recovery,
        "input_ids": [],
        "resource_ids": ["tool:dock-foxtrot"],
    }


@pytest.mark.parametrize("manual_start", [False, True])
@pytest.mark.parametrize("recovery", ["material_selection", "refresh"])
async def test_recoverable_preflight_stays_queued_before_upload_and_preserves_policy(
    db_session, resource_case, monkeypatch, manual_start, recovery
):
    case = resource_case
    case.item.manual_start = manual_start
    case.item.position = 7
    await db_session.commit()
    preflight = AsyncMock(return_value={"issues": [material_issue(recovery)]})
    monkeypatch.setattr(case.plugin, "preflight_job", preflight)
    waiting, failed, toast = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(scheduler_module.notification_service, "on_queue_job_waiting", waiting)
    monkeypatch.setattr(scheduler_module.notification_service, "on_queue_job_failed", failed)
    monkeypatch.setattr(scheduler_module.ws_manager, "send_queue_item_waiting", toast)
    for _ in range(2):
        await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "pending" and case.item.completed_at is None and case.item.started_at is None
    assert case.item.manual_start is manual_start and case.item.position == 7
    assert case.item.error_message is None
    assert case.item.sdk_job["extensions"]["bambuddy:queue_hold"]["kind"] == recovery
    case.upload.assert_not_awaited()
    case.scheduler._preheat_and_soak.assert_not_awaited()
    assert case.plugin.requests == []
    waiting.assert_awaited_once()
    failed.assert_not_awaited()
    assert toast.await_count == 2


async def test_ready_recheck_clears_material_hold_and_dispatches_same_queue_item(
    db_session, resource_case, monkeypatch
):
    case = resource_case
    monkeypatch.setattr(case.plugin, "preflight_job", AsyncMock(return_value={"issues": [material_issue()]}))
    await case.scheduler._start_print(db_session, case.item)
    identity, position = case.item.id, case.item.position
    monkeypatch.setattr(case.plugin, "preflight_job", AsyncMock(return_value={"issues": []}))
    await case.scheduler._start_print(db_session, case.item)
    assert case.item.id == identity and case.item.position == position
    assert case.item.status == "printing"
    assert "bambuddy:queue_hold" not in case.item.sdk_job["extensions"]
    assert case.item.waiting_reason is None
    case.upload.assert_awaited_once()
    assert len(case.plugin.requests) == 1


async def test_changed_physical_material_binding_is_preflighted_before_dispatch(db_session, resource_case, monkeypatch):
    from backend.app.printers.sdk import JobArtifact, JobRequest, MaterialBinding

    case = resource_case
    case.plugin.descriptor = case.plugin.descriptor.model_copy(deep=True)
    for identity in ("feed:empty", "feed:loaded"):
        case.plugin.descriptor.resources.append(
            ResourceDescriptor(
                id=identity,
                kind="material_source",
                label=identity,
                relations={"tools": ["tool:dock-foxtrot"]},
            )
        )
    case.plugin.models = (PrinterModel("future", "Future six-tool printer", device_template=case.plugin.descriptor),)
    plan = JobRequest(
        job_id="selection",
        artifact=JobArtifact(uri="opaque.gcode", format="gcode"),
        selected_tool_ids=["tool:dock-foxtrot"],
        bindings=[MaterialBinding(input_id="material-a", tool_id="tool:dock-foxtrot", source_id="feed:empty")],
    )
    case.item.sdk_job = plan.model_dump(mode="json")
    await db_session.commit()
    checked = []

    async def preflight(config, connection, job, path):
        source = job.bindings[0].source_id
        checked.append(source)
        return {
            "issues": []
            if source == "feed:loaded"
            else [
                {
                    **material_issue(),
                    "input_ids": ["material-a"],
                    "resource_ids": ["feed:empty"],
                }
            ]
        }

    monkeypatch.setattr(case.plugin, "preflight_job", preflight)
    await case.scheduler._start_print(db_session, case.item)
    assert case.item.status == "pending" and checked == ["feed:empty"]
    plan.bindings[0].source_id = "feed:loaded"
    case.item.sdk_job = plan.model_dump(mode="json")
    await db_session.commit()
    await case.scheduler._start_print(db_session, case.item)
    assert checked == ["feed:empty", "feed:loaded"]
    assert case.item.status == "printing"
    assert case.plugin.requests[0].parameters["bindings"][0]["source_id"] == "feed:loaded"
    case.upload.assert_awaited_once()


async def test_preflight_hold_does_not_consume_a_library_source(db_session, resource_case, monkeypatch):
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile

    case = resource_case
    archive = await db_session.get(PrintArchive, case.item.archive_id)
    library = LibraryFile(filename=archive.filename, file_path=archive.file_path, file_size=23, file_type="gcode")
    db_session.add(library)
    await db_session.flush()
    case.item.archive_id, case.item.library_file_id = None, library.id
    case.item.cleanup_library_after_dispatch = True
    await db_session.commit()
    monkeypatch.setattr(case.plugin, "preflight_job", AsyncMock(return_value={"issues": [material_issue()]}))
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.archive_id is None and case.item.library_file_id == library.id
    assert await db_session.get(LibraryFile, library.id) is library
    case.upload.assert_not_awaited()


async def test_preflight_deferral_releases_dispatch_claim_and_unconfirmed_budget(
    test_engine, db_session, resource_case, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    case = resource_case
    monkeypatch.setattr(scheduler_module, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(case.plugin, "preflight_job", AsyncMock(return_value={"issues": [material_issue()]}))
    await case.scheduler._dispatch_one(case.item.id, case.printer.id)
    await db_session.refresh(case.item)
    assert case.item.status == "pending"
    assert case.item.dispatching_at is None
    assert case.item.id not in case.scheduler._unconfirmed_budget_reservations
    assert case.item.id not in case.scheduler._unconfirmed_expected_print
    assert case.item.id not in case.scheduler._unconfirmed_print_user
    case.upload.assert_not_awaited()


async def test_cancellation_during_preflight_cannot_be_resurrected_as_waiting(db_session, resource_case, monkeypatch):
    case = resource_case

    async def cancel(config, connection, job, path):
        case.item.status = "cancelled"
        await db_session.commit()
        return {"issues": [material_issue()]}

    monkeypatch.setattr(case.plugin, "preflight_job", cancel)
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "cancelled"
    assert not case.item.sdk_job
    case.upload.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["ready", "held", "invalid"])
async def test_edited_job_plan_during_preflight_is_preserved_and_revalidated_next_attempt(
    db_session, resource_case, monkeypatch, outcome
):
    from sqlalchemy import update

    case = resource_case

    async def edit(config, connection, job, path):
        await db_session.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == case.item.id)
            .values(job_options={"operator_revision": "new"})
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()
        if outcome == "invalid":
            from backend.app.printers.sdk import SDKValidationError

            raise SDKValidationError("The old job plan was invalid")
        return {"issues": [material_issue()] if outcome == "held" else []}

    monkeypatch.setattr(case.plugin, "preflight_job", edit)
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == "pending"
    assert case.item.job_options == {"operator_revision": "new"}
    assert case.item.sdk_job is None
    assert case.item.waiting_reason is None
    case.upload.assert_not_awaited()
    assert case.plugin.requests == []


@pytest.mark.parametrize("not_submitted", [True, False])
async def test_only_explicit_known_unsent_material_rejection_returns_to_pending(
    db_session, resource_case, monkeypatch, not_submitted
):
    case = resource_case

    def reject(request):
        return OperationReceipt(
            operation_id=request.operation_id,
            request_id=request.request_id,
            status="failed",
            error=OperationError(
                code="job.material_selection_required",
                message="Choose a loaded source",
                retryable=True,
                details={"not_submitted": not_submitted, "issues": [material_issue()]},
            ),
        )

    monkeypatch.setattr(case.plugin, "operation_handlers", lambda connection: {"job.start": reject})
    monkeypatch.setattr(scheduler_module, "delete_file_async", AsyncMock(return_value=True))
    await case.scheduler._start_print(db_session, case.item)
    await db_session.refresh(case.item)
    assert case.item.status == ("pending" if not_submitted else "failed")
    assert (
        bool(case.item.sdk_job and case.item.sdk_job.get("extensions", {}).get("bambuddy:queue_hold")) is not_submitted
    )

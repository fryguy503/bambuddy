"""Resource jobs pass the real scheduler without legacy vendor preparation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.printers.api import PrinterConnectionAdapter
from backend.app.printers.contracts import PrinterConfig, PrinterModel, PrinterPlugin
from backend.app.printers.registry import PrinterPluginRegistry
from backend.app.printers.sdk import DeviceDescriptor, OperationDescriptor, ResourceDescriptor
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

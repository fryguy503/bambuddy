"""A strict API2 callback signature drives real archive start/completion rows."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.printers import registry as registry_module
from backend.app.printers.api import execute_host_command, printer_api
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import DeviceEvent, JobArtifact, JobEventData, JobRequest, OperationError
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_print_complete_end_to_end import _complete
from backend.tests.integration.test_print_start_name_match_ap2 import _start
from examples.printer_plugins.reference.payload.plugin import ReferencePlugin


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_six_tool_typed_events_adopt_and_complete_archive(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch, terminal
):
    class EventPlugin(ReferencePlugin):
        id = "sdk_events_fixture"

        def create_connection(self, config, *, on_event=None):
            return super().create_connection(config, on_event=on_event)

    registry = registry_module.PrinterPluginRegistry()
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(registry_module, "entry_points", lambda **kwargs: [])
    registry.load()
    plugin = EventPlugin()
    registry.register(plugin)
    printer = await printer_factory(plugin_id=plugin.id, model="simulator", plugin_options={"tool_count": 6})
    archive = await archive_factory(
        printer.id, filename="six-tools.gcode", print_name="Six tools", status="pending", with_run=False
    )
    item = PrintQueueItem(printer_id=printer.id, archive_id=archive.id, status="printing")
    db_session.add_all([item, Settings(key="capture_finish_photo", value="false")])
    await db_session.commit()
    starts, completions = [], []
    adapter = printer_api.create_connection(
        PrinterConfig.from_printer(printer),
        on_print_start=starts.append,
        on_print_complete=completions.append,
        on_state_change=lambda state: None,
        unused_callback=MagicMock(side_effect=AssertionError("unsupported callback")),
    )
    monkeypatch.setattr(printer_manager, "_clients", {printer.id: adapter})
    monkeypatch.setattr(printer_manager, "_configs", {printer.id: PrinterConfig.from_printer(printer)})
    job = JobRequest(
        job_id="sdk-job:" + "x" * 120,
        name="Six tools",
        artifact=JobArtifact(uri="host://artifacts/six-tools.gcode", filename="six-tools.gcode", format="gcode"),
        selected_tool_ids=[f"tool:{index}" for index in range(6)],
    )
    item.sdk_job = job.model_dump(mode="json")
    await db_session.commit()
    main._expected_prints[(printer.id, "six-tools.gcode")] = archive.id
    try:
        assert await execute_host_command(adapter, "start_print", job=job, filename="six-tools.gcode")
        assert len(starts) == 1 and "raw_data" not in starts[0]
        await _start(test_engine, printer.id, job.job_id, event_data=starts[0], state_override=adapter.state)
        await db_session.refresh(archive)
        assert (archive.status, archive.subtask_id) == ("printing", job.job_id)

        adapter._connection.state.job.state = terminal
        adapter._connection.state.job.fraction = 1
        if terminal == "failed":
            adapter._connection.state.sequence += 1
            adapter._connection.callbacks["on_event"](
                DeviceEvent(
                    device_id=adapter._connection.descriptor.device_id,
                    sequence=adapter._connection.state.sequence,
                    event="job.failed",
                    data=JobEventData(
                        job=adapter._connection.state.job,
                        artifact=adapter._connection.job_artifact,
                        snapshot=adapter._connection.state,
                        error=OperationError(code="sensor_unavailable", message="Temperature sensor unavailable"),
                    ).model_dump(mode="json"),
                )
            )
        else:
            adapter._connection.emit_job("job." + terminal)
        assert len(completions) == 1 and "raw_data" not in completions[0]
        await _complete(test_engine, printer.id, terminal, event_data=completions[0])
        await db_session.refresh(archive)
        await db_session.refresh(item)
        assert archive.status == terminal
        assert item.status == terminal
        if terminal == "failed":
            assert item.error_message == "Temperature sensor unavailable"
            assert archive.failure_reason == "sensor_unavailable"
        log = await db_session.scalar(select(PrintLogEntry).where(PrintLogEntry.archive_id == archive.id))
        assert log is not None and log.subtask_id == job.job_id
        assert len(completions[0]["sdk_job"]["active_tool_ids"]) == 6
    finally:
        main._expected_prints.clear()
        main._active_prints.clear()
        printer_manager._awaiting_plate_clear.clear()

"""The included provider owns its wire mapping and operating limits."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.printers.bambu.device import describe, handlers
from backend.app.printers.contracts import PrinterConfig
from backend.app.printers.sdk import (
    JobArtifact,
    JobRequest,
    MaterialBinding,
    OperationRequest,
    SDKValidationError,
    validate_device,
    validate_invocation,
)


def test_topology_revision_changes_for_accessory_resources():
    config = PrinterConfig("127.0.0.1", "TEST", "TEST", model="X1C")
    empty = describe(config)
    state = SimpleNamespace(temperatures={}, raw_data={"ams": [{"id": 2, "tray": [{"id": 0}]}]}, nozzles=[])
    attached = describe(config, state=state)
    assert attached.revision != empty.revision
    assert attached.resource("material:ams:2:0").kind == "material_source"
    assert describe(config, state=state).revision == attached.revision


def test_bambu_limits_and_job_options_are_declared_by_the_package():
    device = validate_device(describe(PrinterConfig("127.0.0.1", "TEST", "TEST", model="H2D")))
    validate_invocation(
        device, OperationRequest(operation_id="heater.set_target", resource_id="heater:0", parameters={"target": 320})
    )
    with pytest.raises(SDKValidationError):
        validate_invocation(
            device,
            OperationRequest(operation_id="heater.set_target", resource_id="heater:0", parameters={"target": 321}),
        )
    job = JobRequest(
        job_id="test", artifact=JobArtifact(uri="ready.gcode.3mf", format="3mf"), selected_tool_ids=["tool:0"]
    )
    request = OperationRequest(operation_id="job.start", parameters=job.model_dump(mode="json"))
    validate_invocation(device, request)
    request.parameters["options"] = {"plate_id": 9}
    with pytest.raises(SDKValidationError):
        validate_invocation(device, request)


@pytest.mark.parametrize("input_id", ["0", "-1", "257", "1000000000", "not-a-bambu-input"])
def test_bambu_rejects_invalid_wire_input_indices_before_allocating_mapping(input_id):
    device = describe(PrinterConfig("127.0.0.1", "TEST", "TEST"))
    job = JobRequest(
        job_id="test",
        artifact=JobArtifact(uri="ready.3mf", format="3mf"),
        bindings=[MaterialBinding(input_id=input_id, tool_id="tool:0")],
    )
    with pytest.raises(SDKValidationError):
        validate_invocation(device, OperationRequest(operation_id="job.start", parameters=job.model_dump(mode="json")))


def test_bambu_wire_conversion_stays_inside_operation_handler():
    connection = Mock()
    connection.start_print.return_value = True
    job = JobRequest(
        job_id="test",
        artifact=JobArtifact(uri="ready.3mf", format="3mf"),
        bindings=[MaterialBinding(input_id="1", tool_id="tool:0", source_id="material:ams:2:3")],
        options={"timelapse": True},
    )
    assert handlers(connection)["job.start"](
        OperationRequest(operation_id="job.start", parameters=job.model_dump(mode="json"))
    )
    connection.start_print.assert_called_once_with("ready.3mf", plate_id=1, ams_mapping=[11], timelapse=True)

"""Read-only readiness results refer to actual job inputs and device resources."""

import pytest
from pydantic import ValidationError

from backend.app.printers.job_dispatch import JobDispatchRejected, queue_hold
from backend.app.printers.sdk import (
    DeviceDescriptor,
    JobArtifact,
    JobPreflightResult,
    JobRequest,
    MaterialBinding,
    OperationError,
    OperationReceipt,
    ResourceDescriptor,
    SDKValidationError,
    validate_job_preflight,
)


def job_case():
    device = DeviceDescriptor(
        device_id="opaque",
        resources=[
            ResourceDescriptor(id="tool:a", kind="tool", label="Tool"),
            ResourceDescriptor(id="source:a", kind="material_source", label="Material"),
        ],
    )
    job = JobRequest(
        job_id="job",
        artifact=JobArtifact(uri="source.gcode", format="gcode"),
        bindings=[MaterialBinding(input_id="input-a", tool_id="tool:a", source_id="source:a")],
    )
    issue = {
        "code": "material.empty",
        "message": "Select a loaded material source",
        "recovery": "material_selection",
        "input_ids": ["input-a"],
        "resource_ids": ["source:a"],
    }
    return device, job, issue


def test_empty_issues_are_ready_and_recoverable_issues_make_an_actionable_hold():
    device, job, issue = job_case()
    assert validate_job_preflight(device, job, {"issues": []}).ready
    result = validate_job_preflight(device, job, {"issues": [issue]})
    assert not result.ready
    assert queue_hold(result) == {
        "kind": "material_selection",
        "code": "material.empty",
        "message": issue["message"],
        "input_ids": ["input-a"],
        "resource_ids": ["source:a"],
    }


@pytest.mark.parametrize("field, value", [("input_ids", ["another-input"]), ("resource_ids", ["source:missing"])])
def test_preflight_cannot_name_foreign_inputs_or_resources(field, value):
    device, job, issue = job_case()
    with pytest.raises(SDKValidationError, match="unknown"):
        validate_job_preflight(device, job, {"issues": [{**issue, field: value}]})


def test_preflight_requires_an_explicit_supported_recovery_action():
    _, _, issue = job_case()
    with pytest.raises(ValidationError):
        JobPreflightResult.model_validate({"issues": [{**issue, "recovery": "retry_print"}]})


@pytest.mark.parametrize("proof", [None, False, "true", 1])
def test_retryable_errors_without_known_unsent_proof_are_never_material_holds(proof):
    _, _, issue = job_case()
    receipt = OperationReceipt(
        operation_id="job.start",
        request_id="request",
        status="failed",
        error=OperationError(
            code="job.material_selection_required",
            message="Check material",
            retryable=True,
            details={"not_submitted": proof, "issues": [issue]},
        ),
    )
    assert JobDispatchRejected(receipt).preflight is None


def test_known_unsent_error_needs_valid_nonempty_issues():
    for issues in ([], "invalid"):
        receipt = OperationReceipt(
            operation_id="job.start",
            request_id="request",
            status="failed",
            error=OperationError(
                code="job.material_selection_required",
                message="Check material",
                details={"not_submitted": True, "issues": issues},
            ),
        )
        with pytest.raises(SDKValidationError):
            JobDispatchRejected(receipt)

"""Public API v2 models and conformance checks for printer plugin authors."""

from backend.app.printers.sdk.diagnostics import ConnectionDiagnosticResult, DiagnosticCheck
from backend.app.printers.sdk.domain import (
    SDK_API_VERSION,
    DeviceDescriptor,
    DeviceEvent,
    DeviceSnapshot,
    JobId,
    OperationDescriptor,
    OperationError,
    OperationProgress,
    OperationReceipt,
    OperationRequest,
    ResourceDescriptor,
    ResourceProperty,
    ResourceState,
    TelemetryValue,
)
from backend.app.printers.sdk.host import JobEventData
from backend.app.printers.sdk.jobs import (
    JobArtifact,
    JobDescription,
    JobInput,
    JobProgress,
    JobRequest,
    MaterialBinding,
    MaterialInventoryItem,
    material_inventory,
    validate_job_description,
    validate_job_request,
)
from backend.app.printers.sdk.manifest import PluginManifest
from backend.app.printers.sdk.network_diagnostics import network_diagnostic_checks
from backend.app.printers.sdk.validation import (
    SDKValidationError,
    check_manifest_compatibility,
    manifest_permissions,
    validate_device,
    validate_invocation,
    validate_json_schema,
    validate_manifest,
    validate_plugin_contract,
    validate_receipt,
    validate_snapshot,
    validate_value,
)

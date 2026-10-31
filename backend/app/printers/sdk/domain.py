"""Vendor-neutral device topology, telemetry and operation envelopes.

Resource IDs are opaque stable identifiers. Their position in an array, physical
side and vendor protocol numbers never identify a tool in this API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

SDK_API_VERSION = "2.2.0"
ResourceId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")]
OperationId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:/-]*$")]
JobId = Annotated[str, Field(min_length=1, max_length=256)]
JSONSchema = dict[str, JsonValue]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SDKModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class ResourceProperty(SDKModel):
    """A property with explicit units, schema and optional command binding."""

    label: str | None = None
    value_schema: JSONSchema = Field(default_factory=dict)
    unit: str | None = None
    readable: bool = True
    writable: bool = False
    read_permission: str = "printers:read"
    write_permission: str | None = None
    operation_id: OperationId | None = None
    parameter: str | None = None
    description: str | None = None


class ResourceDescriptor(SDKModel):
    """Any present or future hardware component, without fixed cardinality.

    Common kinds include tool, hotend, extruder, heater, material_source,
    material_slot, material_router, dock, axis, fan, sensor, camera, bed,
    chamber, light, storage, power, probe, filtration and motion_system.
    New kinds can use names such as ``vendor:accessory``.
    """

    id: ResourceId
    kind: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)
    parent_id: ResourceId | None = None
    roles: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    relations: dict[str, list[ResourceId]] = Field(default_factory=dict)
    properties: dict[str, ResourceProperty] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class OperationDescriptor(SDKModel):
    id: OperationId
    label: str = Field(min_length=1, max_length=200)
    description: str | None = None
    namespace: str = "core"
    resource_kinds: list[str] = Field(default_factory=list)
    resource_ids: list[ResourceId] = Field(default_factory=list)
    parameters_schema: JSONSchema = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )
    # A resource can narrow its operation's accepted values (for example each
    # tool heater has its own temperature range) without a new core endpoint.
    resource_parameters_schemas: dict[str, JSONSchema] = Field(default_factory=dict)
    result_schema: JSONSchema = Field(default_factory=dict)
    permission: str = Field(default="printers:control", min_length=1)
    additional_permissions: list[str] = Field(default_factory=list)
    asynchronous: bool = False
    cancellable: bool = False
    idempotent: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=86400)
    confirmation: str | None = None
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid_cancellation(self):
        if self.cancellable and not self.asynchronous:
            raise ValueError("Only asynchronous operations may advertise cancellation")
        if any(not permission for permission in self.additional_permissions):
            raise ValueError("Additional permissions must not be empty")
        if len(self.additional_permissions) != len(set(self.additional_permissions)):
            raise ValueError("Additional permissions must be unique")
        return self


class DeviceDescriptor(SDKModel):
    schema_version: Literal[2] = 2
    device_id: ResourceId
    model_id: str | None = None
    name: str | None = None
    revision: str = "1"
    capabilities: list[str] = Field(default_factory=list)
    resources: list[ResourceDescriptor] = Field(default_factory=list)
    operations: list[OperationDescriptor] = Field(default_factory=list)
    configuration_schema: JSONSchema = Field(default_factory=lambda: {"type": "object"})
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent_topology(self):
        ids = [resource.id for resource in self.resources]
        if len(ids) != len(set(ids)):
            raise ValueError("Resource IDs must be unique within a device")
        operations = {operation.id for operation in self.operations}
        if len(operations) != len(self.operations):
            raise ValueError("Operation IDs must be unique within a device")
        resources = {resource.id: resource for resource in self.resources}
        for resource in self.resources:
            references = [target for targets in resource.relations.values() for target in targets]
            if resource.parent_id is not None:
                references.append(resource.parent_id)
            missing = set(references) - resources.keys()
            if missing:
                raise ValueError(f"Resource {resource.id!r} refers to unknown resources: {sorted(missing)}")
            visited = {resource.id}
            parent = resource.parent_id
            while parent is not None:
                if parent in visited:
                    raise ValueError("Resource parent relationships must not contain cycles")
                visited.add(parent)
                parent = resources[parent].parent_id
            for prop in resource.properties.values():
                if prop.operation_id and prop.operation_id not in operations:
                    raise ValueError(f"Resource property refers to unknown operation {prop.operation_id!r}")
        for operation in self.operations:
            missing = (set(operation.resource_ids) | set(operation.resource_parameters_schemas)) - resources.keys()
            if missing:
                raise ValueError(f"Operation {operation.id!r} refers to unknown resources: {sorted(missing)}")
        return self

    def resource(self, resource_id: str) -> ResourceDescriptor:
        for resource in self.resources:
            if resource.id == resource_id:
                return resource
        raise ValueError(f"Unknown device resource {resource_id!r}")

    def operation(self, operation_id: str) -> OperationDescriptor:
        for operation in self.operations:
            if operation.id == operation_id:
                return operation
        raise ValueError(f"Unsupported device operation {operation_id!r}")


class TelemetryValue(SDKModel):
    value: JsonValue = None
    unit: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    valid_until: datetime | None = None
    quality: Literal["known", "unknown", "stale", "unavailable", "error"] = "known"
    source: str | None = None

    @field_validator("observed_at", "valid_until")
    @classmethod
    def _timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Telemetry timestamps require an explicit timezone")
        return value

    @model_validator(mode="after")
    def _valid_freshness(self):
        if self.valid_until is not None and self.valid_until < self.observed_at:
            raise ValueError("valid_until cannot precede observed_at")
        return self


class ResourceState(SDKModel):
    resource_id: ResourceId
    available: bool | None = None
    status: str | None = None
    properties: dict[str, TelemetryValue] = Field(default_factory=dict)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class JobProgress(SDKModel):
    # A printer may report an active job before exposing a stable native ID.
    # Such observations are display-only; lifecycle events require an ID.
    job_id: JobId | None = None
    state: str
    name: str | None = None
    artifact_uri: str | None = None
    fraction: float | None = Field(default=None, ge=0, le=1)
    phase: str | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    remaining_seconds: float | None = Field(default=None, ge=0)
    layer: int | None = Field(default=None, ge=0)
    total_layers: int | None = Field(default=None, ge=0)
    active_tool_ids: list[ResourceId] = Field(default_factory=list)
    material_usage: dict[str, JsonValue] = Field(default_factory=dict)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class DeviceSnapshot(SDKModel):
    schema_version: Literal[2] = 2
    device_id: ResourceId
    descriptor_revision: str
    sequence: int = Field(default=0, ge=0)
    observed_at: datetime = Field(default_factory=utc_now)
    connected: bool = False
    status: str = "unknown"
    job_id: JobId | None = None
    job: JobProgress | None = None
    resources: list[ResourceState] = Field(default_factory=list)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_state(self):
        if len(self.resources) != len({resource.resource_id for resource in self.resources}):
            raise ValueError("Snapshot resource IDs must be unique")
        if self.observed_at.tzinfo is None:
            raise ValueError("Snapshot timestamps require an explicit timezone")
        return self


class OperationRequest(SDKModel):
    operation_id: OperationId
    resource_id: ResourceId | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class OperationError(SDKModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class OperationProgress(SDKModel):
    fraction: float | None = Field(default=None, ge=0, le=1)
    message: str | None = None
    completed: float | None = Field(default=None, ge=0)
    total: float | None = Field(default=None, ge=0)
    unit: str | None = None


class OperationReceipt(SDKModel):
    operation_id: OperationId
    request_id: str
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled"]
    task_id: str | None = None
    result: JsonValue = None
    error: OperationError | None = None
    progress: OperationProgress | None = None
    accepted_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _coherent_result(self):
        if self.status == "failed" and self.error is None:
            raise ValueError("Failed operations require a structured error")
        if self.status in {"accepted", "running"} and not self.task_id:
            raise ValueError("In-progress operations require a task_id")
        if self.accepted_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Operation timestamps require an explicit timezone")
        if self.updated_at < self.accepted_at:
            raise ValueError("Operation updated_at cannot precede accepted_at")
        if self.error is not None and self.status != "failed":
            raise ValueError("Only failed operations may contain an error")
        return self


class DeviceEvent(SDKModel):
    schema_version: Literal[2] = 2
    device_id: ResourceId
    sequence: int = Field(ge=0)
    event: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=utc_now)
    resource_id: ResourceId | None = None
    request_id: str | None = None
    data: JsonValue = None
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

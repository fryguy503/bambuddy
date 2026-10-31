"""Job plans reference stable resources, independent of tool count or vendor."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated
from uuid import uuid4

from pydantic import Field, JsonValue, model_validator

from backend.app.printers.sdk.domain import (
    DeviceDescriptor,
    DeviceSnapshot,
    JobId,
    JobProgress as JobProgress,
    JSONSchema,
    ResourceId,
    SDKModel,
)
from backend.app.printers.sdk.validation import SDKValidationError


class JobArtifact(SDKModel):
    uri: str = Field(min_length=1, max_length=4096)
    format: str = Field(min_length=1, max_length=128)
    media_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    storage_resource_id: ResourceId | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class MaterialBinding(SDKModel):
    """The material source and physical components assigned to one job input."""

    input_id: str = Field(min_length=1)
    tool_id: ResourceId
    source_id: ResourceId | None = None
    slot_id: ResourceId | None = None
    hotend_id: ResourceId | None = None
    extruder_id: ResourceId | None = None
    material: dict[str, JsonValue] = Field(default_factory=dict)
    required_mass_grams: float | None = Field(default=None, ge=0)
    required_length_mm: float | None = Field(default=None, ge=0)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class JobRequest(SDKModel):
    job_id: JobId
    artifact: JobArtifact
    name: str | None = None
    bindings: list[MaterialBinding] = Field(default_factory=list)
    selected_tool_ids: list[ResourceId] = Field(default_factory=list)
    build_surface_id: ResourceId | None = None
    plate_id: str | None = None
    options: dict[str, JsonValue] = Field(default_factory=dict)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_inputs(self):
        if len(self.bindings) != len({binding.input_id for binding in self.bindings}):
            raise ValueError("Each job material input may be bound only once")
        if len(self.selected_tool_ids) != len(set(self.selected_tool_ids)):
            raise ValueError("Selected tool IDs must be unique")
        return self


class JobInput(SDKModel):
    """A sliced input whose identity and equivalence are owned by its plugin."""

    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)
    artifact_uri: str | None = Field(default=None, max_length=4096)
    routing_key: str | None = Field(default=None, min_length=1, max_length=512)
    material_type: str | None = None
    color: str | None = None
    required_mass_grams: float | None = Field(default=None, ge=0)
    compatible_tool_ids: list[ResourceId] = Field(default_factory=list)
    compatible_source_ids: list[ResourceId] = Field(default_factory=list)
    required: bool = True


class JobDescription(SDKModel):
    artifact: JobArtifact
    inputs: list[JobInput] = Field(default_factory=list)
    options_schema: JSONSchema = Field(default_factory=lambda: {"type": "object", "properties": {}})
    default_options: dict[str, JsonValue] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    extensions: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_inputs(self):
        if len(self.inputs) != len({item.id for item in self.inputs}):
            raise ValueError("Job input IDs must be unique within an artifact")
        return self


def validate_job_description(device: DeviceDescriptor, value) -> JobDescription:
    from backend.app.printers.sdk.validation import validate_json_schema

    description = value if isinstance(value, JobDescription) else JobDescription.model_validate(value)
    validate_json_schema(description.options_schema)
    # Defaults may be partial: required choices can need explicit user input.
    # The complete job options remain subject to job.start schema validation.
    for item in description.inputs:
        for resource_id in item.compatible_tool_ids:
            if device.resource(resource_id).kind != "tool":
                raise SDKValidationError("Job input references a non-tool resource")
        for resource_id in item.compatible_source_ids:
            if device.resource(resource_id).kind != "material_source":
                raise SDKValidationError("Job input references a non-material resource")
        if item.artifact_uri is not None and item.artifact_uri != description.artifact.uri:
            raise SDKValidationError("Job input belongs to another artifact")
    return description


class MaterialInventoryItem(SDKModel):
    source_id: ResourceId
    tool_ids: list[ResourceId] = Field(default_factory=list)
    material_type: str | None = None
    color: str | None = None
    material_id: str | None = None
    spool_id: str | None = None
    remaining_mass_grams: float | None = Field(default=None, ge=0)
    remaining_length_mm: float | None = Field(default=None, ge=0)
    available: bool | None = None
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


def material_inventory(device: DeviceDescriptor, snapshot: DeviceSnapshot) -> list[MaterialInventoryItem]:
    """Read normalized material sources without packed numeric slot conventions."""
    from backend.app.printers.sdk.validation import validate_snapshot

    validate_snapshot(device, snapshot)
    states = {state.resource_id: state for state in snapshot.resources}
    result = []
    for source in device.resources:
        if source.kind not in {"material_source", "material_slot", "spool", "feeder"}:
            continue
        state = states.get(source.id)
        if state is None:
            continue
        properties = {name: value.value for name, value in state.properties.items() if value.quality == "known"}
        tools = set(source.relations.get("tools", []))
        tools.update(
            tool.id
            for tool in device.resources
            if tool.kind == "tool" and source.id in tool.relations.get("material_sources", [])
        )
        fields = {
            key: properties[key]
            for key in (
                "material_type",
                "color",
                "material_id",
                "spool_id",
                "remaining_mass_grams",
                "remaining_length_mm",
            )
            if key in properties
        }
        result.append(
            MaterialInventoryItem.model_validate(
                {
                    "source_id": source.id,
                    "tool_ids": sorted(tools),
                    "available": state.available,
                    **fields,
                }
            )
        )
    return result


def validate_job_request(device: DeviceDescriptor, value: JobRequest | dict) -> JobRequest:
    job = value if isinstance(value, JobRequest) else JobRequest.model_validate(value)
    resources = {resource.id: resource for resource in device.resources}

    def require(resource_id, kind):
        if resource_id is None:
            return
        resource = resources.get(resource_id)
        if resource is None or resource.kind not in kind:
            raise SDKValidationError("Job references a missing resource or an incompatible resource kind")

    for tool_id in job.selected_tool_ids:
        require(tool_id, {"tool"})
    require(job.build_surface_id, {"bed", "build_surface"})
    require(job.artifact.storage_resource_id, {"storage"})
    for binding in job.bindings:
        require(binding.tool_id, {"tool"})
        if job.selected_tool_ids and binding.tool_id not in job.selected_tool_ids:
            raise SDKValidationError("Job material binding uses a tool outside the selected tool plan")
        require(binding.source_id, {"material_source", "material_slot", "feeder", "spool"})
        require(binding.slot_id, {"material_slot"})
        require(binding.hotend_id, {"hotend"})
        require(binding.extruder_id, {"extruder"})
        # A descriptor can explicitly name compatible feeding paths. Missing
        # topology is unknown; a declared relation must not be contradicted.
        tool = resources[binding.tool_id]
        for field, relation in (("hotend_id", "hotends"), ("extruder_id", "extruders")):
            selected = getattr(binding, field)
            declared = tool.relations.get(relation)
            if selected is not None and declared is not None and selected not in declared:
                raise SDKValidationError("Job material binding contradicts the device topology")
        if binding.source_id is not None:
            source = resources[binding.source_id]
            for resource, relation, selected in (
                (tool, "material_sources", source.id),
                (source, "tools", tool.id),
            ):
                declared = resource.relations.get(relation)
                if declared is not None and selected not in declared:
                    raise SDKValidationError("Job material binding contradicts the device topology")
            if binding.slot_id is not None and binding.slot_id != source.id:
                slot = resources[binding.slot_id]
                if slot.parent_id is not None and slot.parent_id != source.id:
                    raise SDKValidationError("Job material slot belongs to a different source")
                for resource, relation, selected in (
                    (source, "slots", slot.id),
                    (source, "material_slots", slot.id),
                    (slot, "sources", source.id),
                    (slot, "material_sources", source.id),
                ):
                    declared = resource.relations.get(relation)
                    if declared is not None and selected not in declared:
                        raise SDKValidationError("Job material slot contradicts the device topology")
    return job


def job_from_host_command(
    device: DeviceDescriptor, filename: str | None = None, *args, job=None, **kwargs
) -> JobRequest:
    """Adapt a host dispatch only when its resource identity is unambiguous.

    A saved API2 job plan is the normal path for multiple tools/materials.
    Legacy numeric mappings cannot identify arbitrary topology and are refused.
    The fallback serves a simple one-tool printer without fabricating mappings.
    """
    if job is not None:
        result = job if isinstance(job, JobRequest) else JobRequest.model_validate(job)
        if filename:
            result = result.model_copy(update={"artifact": result.artifact.model_copy(update={"uri": filename})})
        return result
    if args:
        raise SDKValidationError("API2 host dispatch requires named job fields or a saved JobRequest")
    if not filename:
        raise SDKValidationError("A job artifact is required")
    if kwargs.get("ams_mapping") or kwargs.get("nozzle_mapping"):
        raise SDKValidationError("API2 multi-material dispatch requires stable resource bindings in a JobRequest")
    tools = [resource.id for resource in device.resources if resource.kind == "tool"]
    if len(tools) > 1:
        raise SDKValidationError("API2 multi-tool dispatch requires an explicit JobRequest tool plan")
    options = kwargs.get("options") or {}
    if not isinstance(options, dict):
        raise SDKValidationError("Job options must be an object")
    # The manifest's operation schema determines supported options. No Bambu
    # calibration/speed/AMS setting is silently translated for another vendor.
    return JobRequest(
        job_id=kwargs.get("job_id") or str(uuid4()),
        name=kwargs.get("subtask_name"),
        artifact=JobArtifact(uri=filename, format=PurePosixPath(filename).suffix.removeprefix(".") or "gcode"),
        selected_tool_ids=tools,
        plate_id=str(kwargs["plate_id"]) if kwargs.get("plate_id") is not None else None,
        options=options,
    )

"""Conformance of API2 resources without any actual printer implementation."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.printers.sdk import (
    DeviceDescriptor,
    DeviceSnapshot,
    JobArtifact,
    JobProgress,
    JobRequest,
    MaterialBinding,
    OperationDescriptor,
    OperationReceipt,
    OperationRequest,
    PluginManifest,
    ResourceDescriptor,
    ResourceProperty,
    ResourceState,
    SDKValidationError,
    TelemetryValue,
    check_manifest_compatibility,
    manifest_permissions,
    validate_device,
    validate_invocation,
    validate_job_request,
    validate_json_schema,
    validate_plugin_contract,
    validate_receipt,
    validate_snapshot,
    validate_value,
)


def toolchanger(count=6):
    resources = []
    for index in range(count):
        resources.extend(
            [
                ResourceDescriptor(
                    id=f"tool:{index}",
                    kind="tool",
                    label=f"Tool {index}",
                    relations={"hotends": [f"hotend:{index}"], "extruders": [f"extruder:{index}"]},
                ),
                ResourceDescriptor(id=f"hotend:{index}", kind="hotend", label="Hotend"),
                ResourceDescriptor(id=f"extruder:{index}", kind="extruder", label="Extruder"),
                ResourceDescriptor(
                    id=f"heater:{index}",
                    kind="heater",
                    label="Heater",
                    relations={"heats": [f"hotend:{index}"]},
                    properties={
                        "target": ResourceProperty(
                            value_schema={"type": "number", "minimum": 0, "maximum": 450 + index},
                            unit="degC",
                            writable=True,
                            operation_id="heater.set_target",
                            parameter="target",
                        )
                    },
                ),
                ResourceDescriptor(
                    id=f"dock:{index}", kind="dock", label="Dock", relations={"accepts": [f"tool:{index}"]}
                ),
            ]
        )
    return DeviceDescriptor(
        device_id="test-toolchanger",
        resources=resources,
        operations=[
            OperationDescriptor(
                id="heater.set_target",
                label="Set target",
                resource_kinds=["heater"],
                parameters_schema={
                    "type": "object",
                    "properties": {"target": {"type": "number"}},
                    "required": ["target"],
                    "additionalProperties": False,
                },
                result_schema={"type": "boolean"},
            )
        ],
    )


@pytest.mark.parametrize("count", [1, 3, 6, 17, 32])
def test_arbitrary_tool_topology_has_no_two_tool_ceiling(count):
    device = validate_device(toolchanger(count))
    assert sum(resource.kind == "tool" for resource in device.resources) == count
    request = OperationRequest(
        operation_id="heater.set_target", resource_id=f"heater:{count - 1}", parameters={"target": 440}
    )
    validate_invocation(device, request)


def test_each_resource_enforces_its_own_limits_and_identity():
    device = toolchanger()
    validate_invocation(
        device, OperationRequest(operation_id="heater.set_target", resource_id="heater:5", parameters={"target": 454})
    )
    with pytest.raises(SDKValidationError):
        validate_invocation(
            device,
            OperationRequest(operation_id="heater.set_target", resource_id="heater:0", parameters={"target": 454}),
        )
    with pytest.raises(SDKValidationError):
        validate_invocation(
            device, OperationRequest(operation_id="heater.set_target", resource_id="tool:0", parameters={"target": 100})
        )
    with pytest.raises(ValueError):
        validate_invocation(
            device,
            OperationRequest(operation_id="heater.set_target", resource_id="heater:99", parameters={"target": 100}),
        )


def test_topology_rejects_duplicate_dangling_and_cyclic_resources():
    with pytest.raises(ValidationError):
        DeviceDescriptor(
            device_id="x",
            resources=[
                ResourceDescriptor(id="x", kind="tool", label="X"),
                ResourceDescriptor(id="x", kind="tool", label="X"),
            ],
        )
    with pytest.raises(ValidationError):
        DeviceDescriptor(
            device_id="x",
            resources=[ResourceDescriptor(id="x", kind="tool", label="X", relations={"uses": ["absent"]})],
        )
    with pytest.raises(ValidationError):
        DeviceDescriptor(
            device_id="x",
            resources=[
                ResourceDescriptor(id="x", kind="tool", label="X", parent_id="y"),
                ResourceDescriptor(id="y", kind="dock", label="Y", parent_id="x"),
            ],
        )


def test_new_resource_kinds_and_extensions_do_not_require_core_enumeration():
    device = DeviceDescriptor(
        device_id="future",
        resources=[
            ResourceDescriptor(
                id="future:actuator",
                kind="vendor:novel_actuator",
                label="New actuator",
                extensions={"vendor:attributes": {"motion": [1, 2, 3]}},
            )
        ],
    )
    assert validate_device(device).resources[0].kind == "vendor:novel_actuator"


def test_schemas_are_local_valid_and_do_not_echo_credentials():
    validate_json_schema(
        {
            "type": "object",
            "$defs": {"token": {"type": "string", "minLength": 4}},
            "properties": {"token": {"$ref": "#/$defs/token"}},
        }
    )
    for schema in [
        {"$ref": "https://attacker.invalid/schema"},
        {"$ref": "#/$defs/missing"},
        {"type": "nonsense"},
        {"$id": "https://attacker.invalid/root", "type": "object"},
    ]:
        with pytest.raises(SDKValidationError):
            validate_json_schema(schema)
    with pytest.raises(SDKValidationError) as error:
        validate_value({"password": "SECRET"}, {"type": "object", "properties": {"password": {"type": "integer"}}})
    assert "SECRET" not in str(error.value)


def test_manifest_compatibility_negotiates_core_api_python_and_dependencies():
    manifest = PluginManifest(
        plugin_id="example",
        name="Example",
        version="1.2.3",
        publisher_id="example",
        entrypoint="plugin:Factory",
        requires_core=">=1.3.1b1,<2",
        dependencies={"jsonschema": ">=4,<5"},
    )
    check_manifest_compatibility(manifest, core_version="1.3.1b1", installed_dependencies={"jsonschema": "4.26.0"})
    for options in [
        {"core_version": "1.2.0"},
        {"core_version": "1.3.1b1", "api_version": "3.0.0"},
        {"core_version": "1.3.1b1", "python_version": "3.10.0"},
        {"core_version": "1.3.1b1", "installed_dependencies": {"jsonschema": "3.0.0"}},
    ]:
        with pytest.raises(SDKValidationError):
            check_manifest_compatibility(manifest, **options)


def test_anonymous_job_observations_require_sdk_2_1_but_existing_v2_plugins_remain_compatible():
    from backend.app.printers.sdk import SDK_API_VERSION

    manifest = PluginManifest(
        plugin_id="observer",
        name="Observer",
        version="1.0.0",
        publisher_id="example",
        entrypoint="plugin:Factory",
        requires_api=">=2.1.0,<3.0.0",
    )
    assert SDK_API_VERSION == "2.1.0"
    with pytest.raises(SDKValidationError, match="unsupported printer API"):
        check_manifest_compatibility(manifest, core_version="1.3.1b1", api_version="2.0.0")
    check_manifest_compatibility(manifest, core_version="1.3.1b1")
    manifest.requires_api = ">=2.0.0,<3.0.0"
    check_manifest_compatibility(manifest, core_version="1.3.1b1")


@pytest.mark.parametrize("path", ["../evil.py", "nested/../evil.py", "/absolute.py", "C:/evil.py", "nested\\evil.py"])
def test_manifest_inventory_rejects_unsafe_paths(path):
    with pytest.raises(ValidationError):
        PluginManifest(
            plugin_id="example",
            name="Example",
            version="1.0.0",
            publisher_id="example",
            entrypoint="plugin:Factory",
            files={path: "0" * 64},
        )


def test_snapshot_uses_stable_ids_units_freshness_and_revision():
    device = toolchanger()
    snapshot = DeviceSnapshot(
        device_id=device.device_id,
        descriptor_revision=device.revision,
        resources=[
            ResourceState(resource_id="heater:5", properties={"target": TelemetryValue(value=450, unit="degC")})
        ],
    )
    validate_snapshot(device, snapshot)
    with pytest.raises(SDKValidationError):
        validate_snapshot(device, snapshot.model_copy(update={"descriptor_revision": "other"}))
    bad_unit = snapshot.model_copy(deep=True)
    bad_unit.resources[0].properties["target"].unit = "degF"
    with pytest.raises(SDKValidationError):
        validate_snapshot(device, bad_unit)
    with pytest.raises(ValidationError):
        TelemetryValue(
            value=1,
            observed_at=datetime.now(timezone.utc),
            valid_until=datetime.now(timezone.utc) - timedelta(seconds=5),
        )


def test_async_receipts_require_tasks_and_matching_result_schemas():
    request = OperationRequest(operation_id="heater.set_target", resource_id="heater:0", parameters={"target": 200})
    operation = toolchanger().operations[0]
    good = OperationReceipt(
        operation_id=request.operation_id, request_id=request.request_id, status="succeeded", result=True
    )
    validate_receipt(operation, request, good)
    with pytest.raises(SDKValidationError):
        validate_receipt(operation, request, good.model_copy(update={"result": "not a boolean"}))
    with pytest.raises(ValidationError):
        OperationReceipt(operation_id=request.operation_id, request_id=request.request_id, status="running")
    pending = OperationReceipt(
        operation_id=request.operation_id, request_id=request.request_id, status="running", task_id="task-1"
    )
    with pytest.raises(SDKValidationError):
        validate_receipt(operation, request, pending)


def test_arbitrary_tool_job_bindings_preserve_resource_relationships():
    device = toolchanger(8)
    job = JobRequest(
        job_id="job-1",
        artifact=JobArtifact(uri="host://artifact/1", format="gcode"),
        selected_tool_ids=["tool:7"],
        bindings=[
            MaterialBinding(input_id="material-1", tool_id="tool:7", hotend_id="hotend:7", extruder_id="extruder:7")
        ],
    )
    assert validate_job_request(device, job).bindings[0].tool_id == "tool:7"
    bad = job.model_copy(deep=True)
    bad.bindings[0].hotend_id = "hotend:0"
    with pytest.raises(SDKValidationError):
        validate_job_request(device, bad)


def test_material_paths_cannot_contradict_selected_tools_sources_or_slots():
    device = toolchanger(8)
    device.resources.extend(
        [
            ResourceDescriptor(id="feed:a", kind="material_source", label="A", relations={"tools": ["tool:7"]}),
            ResourceDescriptor(id="feed:b", kind="material_source", label="B"),
            ResourceDescriptor(id="slot:a", kind="material_slot", label="A slot", parent_id="feed:a"),
            ResourceDescriptor(id="slot:b", kind="material_slot", label="B slot", parent_id="feed:b"),
        ]
    )
    device.resource("tool:7").relations["material_sources"] = ["feed:a"]
    good = JobRequest(
        job_id="bound",
        artifact=JobArtifact(uri="host://artifact/1", format="gcode"),
        selected_tool_ids=["tool:7"],
        bindings=[MaterialBinding(input_id="a", tool_id="tool:7", source_id="feed:a", slot_id="slot:a")],
    )
    validate_job_request(device, good)
    for change in ({"tool_id": "tool:0"}, {"source_id": "feed:b"}, {"slot_id": "slot:b"}):
        bad = good.model_copy(deep=True)
        bad.bindings[0] = bad.bindings[0].model_copy(update=change)
        with pytest.raises(SDKValidationError):
            validate_job_request(device, bad)
    # Both directions are authoritative when explicitly declared.
    device.resource("feed:a").relations["tools"] = ["tool:0"]
    with pytest.raises(SDKValidationError):
        validate_job_request(device, good)


def test_host_projection_is_vendor_independent_and_tracks_all_resource_values():
    from types import SimpleNamespace

    from backend.app.printers.sdk.host import HostPrinterState, state_for_host
    from backend.app.printers.state_accessors import normalized_state_key

    device = toolchanger(6)
    config = SimpleNamespace(plugin_id="future_driver")
    plugin = SimpleNamespace(describe_device=lambda config: device)
    snapshot = DeviceSnapshot(
        device_id=device.device_id,
        descriptor_revision=device.revision,
        resources=[
            ResourceState(resource_id="heater:5", properties={"target": TelemetryValue(value=100, unit="degC")})
        ],
    )
    state = state_for_host(config, plugin, snapshot)
    assert type(state) is HostPrinterState
    assert state.nozzles == [] and state.fila_switch is None and state.raw_data == {}
    assert state.temperatures["heater:5_target"] == 100
    key = normalized_state_key(state)
    heartbeat = snapshot.model_copy(deep=True)
    heartbeat.sequence += 1
    heartbeat.observed_at += timedelta(seconds=1)
    heartbeat.resources[0].properties["target"].observed_at += timedelta(seconds=1)
    assert normalized_state_key(state_for_host(config, plugin, heartbeat)) == key
    changed = heartbeat.model_copy(deep=True)
    changed.resources[0].properties["target"].value = 200
    assert normalized_state_key(state_for_host(config, plugin, changed)) != key
    changed = heartbeat.model_copy(deep=True)
    changed.resources[0].properties["target"].quality = "stale"
    assert normalized_state_key(state_for_host(config, plugin, changed)) != key


def test_static_model_templates_validate_schemas_and_every_permission_without_connecting():
    from backend.app.printers.contracts import PrinterModel, PrinterPlugin

    template = toolchanger()
    template.resource("heater:5").properties["target"].read_permission = "printers:update"

    class TemplatePlugin(PrinterPlugin):
        id = "template_test"
        name = "Template test"
        version = "1.0.0"
        api_version = 2
        models = (PrinterModel("future", "Future", device_template=template),)

        def create_connection(self, config, **callbacks):
            raise AssertionError("Static conformance must not open a connection")

    plugin = TemplatePlugin()
    assert "printers:update" in manifest_permissions(plugin.sdk_manifest())
    with pytest.raises(SDKValidationError):
        validate_plugin_contract(plugin, known_permissions={"printers:read", "printers:control"})
    validate_plugin_contract(plugin, known_permissions={"printers:read", "printers:control", "printers:update"})
    template.resource("heater:5").properties["target"].value_schema = {"$ref": "https://invalid.example/schema"}
    with pytest.raises(SDKValidationError):
        validate_plugin_contract(plugin)


@pytest.mark.parametrize("length", [1, 127, 256])
def test_job_identity_limits_match_persisted_archive_and_run_keys(length):
    identity = "j" * length
    assert JobRequest(job_id=identity, artifact=JobArtifact(uri="host://artifact/1", format="gcode")).job_id == identity
    assert JobProgress(job_id=identity, state="idle").job_id == identity
    assert DeviceSnapshot(device_id="device", descriptor_revision="1", job_id=identity).job_id == identity


@pytest.mark.parametrize("identity", ["", "j" * 257])
def test_invalid_job_identities_are_rejected_at_each_sdk_boundary(identity):
    for factory in (
        lambda: JobRequest(job_id=identity, artifact=JobArtifact(uri="host://artifact/1", format="gcode")),
        lambda: JobProgress(job_id=identity, state="idle"),
        lambda: DeviceSnapshot(device_id="device", descriptor_revision="1", job_id=identity),
    ):
        with pytest.raises(ValidationError):
            factory()

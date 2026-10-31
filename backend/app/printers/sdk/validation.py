"""Conformance checks for installed packages and the live addon boundary."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from backend.app.printers.sdk.domain import (
    SDK_API_VERSION,
    DeviceDescriptor,
    DeviceSnapshot,
    OperationDescriptor,
    OperationReceipt,
    OperationRequest,
)
from backend.app.printers.sdk.manifest import PluginManifest


class SDKValidationError(ValueError):
    def __init__(self, message: str, *, path: str | None = None):
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


def validate_json_schema(schema: Mapping, *, path: str = "schema") -> None:
    """Only self-contained Draft 2020-12 schemas; validation never uses network IO."""
    if schema.get("$schema") not in (None, "https://json-schema.org/draft/2020-12/schema"):
        raise SDKValidationError("Only JSON Schema Draft 2020-12 is supported", path=path)

    references = []
    anchors = set()

    def visit(value, depth=0):
        if depth > 64:
            raise SDKValidationError("Schema nesting exceeds 64 levels", path=path)
        if isinstance(value, dict):
            if "$id" in value:
                raise SDKValidationError("Schema $id scopes are unsupported; use local $defs", path=path)
            for key in ("$ref", "$dynamicRef"):
                if key in value and (not isinstance(value[key], str) or not value[key].startswith("#")):
                    raise SDKValidationError("External schema references are not permitted", path=path)
                if key in value:
                    references.append(value[key])
            for key in ("$anchor", "$dynamicAnchor"):
                if key in value:
                    anchors.add(value[key])
            for item in value.values():
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)

    visit(dict(schema))
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise SDKValidationError(exc.message, path=path) from exc
    from urllib.parse import unquote

    for reference in references:
        fragment = unquote(reference[1:])
        if not fragment:
            continue
        if not fragment.startswith("/"):
            if fragment not in anchors:
                raise SDKValidationError("Schema anchor cannot be resolved locally", path=path)
            continue
        current = schema
        try:
            for token in fragment[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                current = current[int(token)] if isinstance(current, list) else current[token]
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise SDKValidationError("Schema reference cannot be resolved locally", path=path) from exc


def validate_value(value: Any, schema: Mapping, *, path: str = "value") -> None:
    validate_json_schema(schema, path=path)
    try:
        errors = sorted(
            Draft202012Validator(dict(schema), format_checker=FormatChecker()).iter_errors(value),
            key=lambda error: str(error.json_path),
        )
    except Exception as exc:
        raise SDKValidationError("Schema reference cannot be resolved locally", path=path) from exc
    if errors:
        # Deliberately do not echo values: config and operation arguments can
        # contain credentials. Callers receive a location and a constraint.
        error = errors[0]
        raise SDKValidationError(f"Does not satisfy {error.validator} constraint", path=f"{path}{error.json_path[1:]}")


def validate_manifest(value: PluginManifest | Mapping, **compatibility) -> PluginManifest:
    manifest = value if isinstance(value, PluginManifest) else PluginManifest.model_validate(value)
    validate_json_schema(manifest.configuration_schema, path="configuration_schema")
    ids = [operation.id for operation in manifest.operations]
    if len(ids) != len(set(ids)):
        raise SDKValidationError("Manifest operation IDs must be unique")
    for operation in manifest.operations:
        validate_operation_descriptor(operation)
    model_ids = set()
    for model in manifest.models:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in model_ids:
            raise SDKValidationError("Manifest models require unique nonempty IDs")
        model_ids.add(model_id)
        template = model.get("device_template")
        if template is not None:
            if not isinstance(template, dict):
                raise SDKValidationError("Model device_template must be a device descriptor")
            descriptor = validate_device(template)
            if descriptor.model_id not in {None, model_id}:
                raise SDKValidationError("Model template identity does not match its catalog entry")
    if compatibility:
        check_manifest_compatibility(manifest, **compatibility)
    return manifest


def manifest_permissions(manifest: PluginManifest) -> set[str]:
    """Every permission declared by operations or static model telemetry."""
    operations = list(manifest.operations)
    permissions = set()
    for model in manifest.models:
        template = model.get("device_template")
        if isinstance(template, dict):
            device = validate_device(template)
            operations.extend(device.operations)
            permissions.update(
                permission
                for resource in device.resources
                for prop in resource.properties.values()
                for permission in (prop.read_permission, prop.write_permission)
                if permission is not None
            )
    permissions.update(
        permission
        for operation in operations
        for permission in (operation.permission, *operation.additional_permissions)
    )
    return permissions


def check_manifest_compatibility(
    manifest: PluginManifest,
    *,
    core_version: str,
    api_version: str = SDK_API_VERSION,
    python_version: str | None = None,
    supported_api_versions: tuple[str, ...] | None = None,
    installed_dependencies: Mapping[str, str] | None = None,
) -> None:
    versions = supported_api_versions or (api_version,)
    api_range = SpecifierSet(manifest.requires_api)
    if not any(
        Version(version).major == manifest.api_version and api_range.contains(version, prereleases=True)
        for version in versions
    ):
        raise SDKValidationError("Plugin requires an unsupported printer API version")
    if not SpecifierSet(manifest.requires_core).contains(core_version, prereleases=True):
        raise SDKValidationError("Plugin does not support this Bambuddy core version")
    python_version = python_version or ".".join(str(part) for part in sys.version_info[:3])
    if not SpecifierSet(manifest.requires_python).contains(python_version, prereleases=True):
        raise SDKValidationError("Plugin does not support this Python version")
    if installed_dependencies is not None:
        normalized = {
            name.lower().replace("_", "-").replace(".", "-"): version
            for name, version in installed_dependencies.items()
        }
        for package, required in manifest.dependencies.items():
            version = normalized.get(package.lower().replace("_", "-").replace(".", "-"))
            if version is None or not SpecifierSet(required).contains(version, prereleases=True):
                raise SDKValidationError(f"Plugin dependency {package!r} does not satisfy {required!r}")


def validate_operation_descriptor(operation: OperationDescriptor) -> OperationDescriptor:
    validate_json_schema(operation.parameters_schema, path=f"operations.{operation.id}.parameters_schema")
    validate_json_schema(operation.result_schema, path=f"operations.{operation.id}.result_schema")
    for resource_id, schema in operation.resource_parameters_schemas.items():
        validate_json_schema(schema, path=f"operations.{operation.id}.resources.{resource_id}")
    return operation


def validate_device(value: DeviceDescriptor | Mapping) -> DeviceDescriptor:
    device = value if isinstance(value, DeviceDescriptor) else DeviceDescriptor.model_validate(value)
    validate_json_schema(device.configuration_schema, path="configuration_schema")
    for operation in device.operations:
        validate_operation_descriptor(operation)
    for resource in device.resources:
        for name, prop in resource.properties.items():
            validate_json_schema(prop.value_schema, path=f"resources.{resource.id}.{name}")
    return device


def validate_invocation(device: DeviceDescriptor, request: OperationRequest) -> OperationDescriptor:
    operation = device.operation(request.operation_id)
    if operation.resource_ids or operation.resource_kinds:
        if request.resource_id is None:
            raise SDKValidationError("This operation requires a resource_id")
    if request.resource_id is not None:
        resource = device.resource(request.resource_id)
        if operation.resource_ids and resource.id not in operation.resource_ids:
            raise SDKValidationError("Operation is not supported by the selected resource")
        if operation.resource_kinds and resource.kind not in operation.resource_kinds:
            raise SDKValidationError("Operation does not support this resource kind")
        override = operation.resource_parameters_schemas.get(resource.id)
        if override is not None:
            validate_value(request.parameters, override, path="parameters")
        for prop in resource.properties.values():
            if (
                prop.operation_id == operation.id
                and prop.parameter is not None
                and prop.parameter in request.parameters
            ):
                validate_value(
                    request.parameters[prop.parameter], prop.value_schema, path=f"parameters.{prop.parameter}"
                )
    validate_value(request.parameters, operation.parameters_schema, path="parameters")
    return operation


def validate_snapshot(device: DeviceDescriptor, value: DeviceSnapshot | Mapping) -> DeviceSnapshot:
    snapshot = value if isinstance(value, DeviceSnapshot) else DeviceSnapshot.model_validate(value)
    if snapshot.device_id != device.device_id or snapshot.descriptor_revision != device.revision:
        raise SDKValidationError("Snapshot identity or descriptor revision does not match the device")
    if snapshot.job is not None and snapshot.job_id is not None and snapshot.job_id != snapshot.job.job_id:
        raise SDKValidationError("Snapshot and job identify different jobs")
    for state in snapshot.resources:
        resource = device.resource(state.resource_id)
        for key, telemetry in state.properties.items():
            prop = resource.properties.get(key)
            if prop is None:
                raise SDKValidationError("Undeclared telemetry property", path=f"resources.{state.resource_id}.{key}")
            if telemetry.quality == "known":
                validate_value(telemetry.value, prop.value_schema, path=f"resources.{state.resource_id}.{key}")
            if telemetry.unit and prop.unit and telemetry.unit != prop.unit:
                raise SDKValidationError("Telemetry unit does not match the descriptor")
    return snapshot


def validate_receipt(operation: OperationDescriptor, request: OperationRequest, value) -> OperationReceipt:
    receipt = value if isinstance(value, OperationReceipt) else OperationReceipt.model_validate(value)
    if receipt.operation_id != request.operation_id or receipt.request_id != request.request_id:
        raise SDKValidationError("Operation receipt does not match the request")
    if receipt.status in {"accepted", "running"} and not operation.asynchronous:
        raise SDKValidationError("Synchronous operation returned a running task")
    if receipt.status == "succeeded":
        validate_value(receipt.result, operation.result_schema, path="result")
    return receipt


def validate_plugin_contract(
    plugin, manifest: PluginManifest | Mapping | None = None, *, core_version=None, known_permissions=None
) -> PluginManifest:
    """Static installation checks: no network or hardware connection is opened."""
    from backend.app.printers.contracts import PrinterConfig, PrinterPlugin

    if not isinstance(plugin, PrinterPlugin):
        raise SDKValidationError("Plugin factory must return a PrinterPlugin")
    metadata = validate_manifest(plugin.sdk_manifest())
    manifest = validate_manifest(manifest) if manifest is not None else metadata
    # Installed packages have authoritative pre-import JSON metadata. A driver
    # may additionally declare code metadata, which must agree on ownership.
    if plugin.manifest_data is not None:
        for name in ("plugin_id", "version", "api_version", "publisher_id"):
            if getattr(metadata, name) != getattr(manifest, name):
                raise SDKValidationError(f"Plugin code and static manifest disagree on {name}")
    if (
        plugin.id != manifest.plugin_id
        or plugin.version != manifest.version
        or plugin.api_version != manifest.api_version
    ):
        raise SDKValidationError("Plugin instance identity/version does not match its manifest")
    if known_permissions is not None:
        declared = manifest_permissions(manifest)
        if declared - set(known_permissions):
            raise SDKValidationError("Plugin declares permissions this core does not support")
    placeholder = PrinterConfig("", "", "", plugin_id=plugin.id)
    try:
        inspect.signature(plugin.create_connection).bind(placeholder)
        for hook in (plugin.start_host_services, plugin.stop_host_services):
            inspect.signature(hook).bind(object())
            if not inspect.iscoroutinefunction(hook):
                raise SDKValidationError("Plugin host service hooks must be async functions")
        if manifest.api_version == 2:
            inspect.signature(plugin.describe_device).bind(placeholder)
            inspect.signature(plugin.describe_job).bind(placeholder, object(), object())
            inspect.signature(plugin.connection_diagnostics).bind(
                placeholder, connection=None, wait_for_publish_seconds=0.0, progress=None
            )
            inspect.signature(plugin.snapshot_device).bind(placeholder, object())
            inspect.signature(plugin.invoke).bind(
                placeholder, object(), OperationRequest(operation_id="conformance.check")
            )
            if type(plugin).describe_device is PrinterPlugin.describe_device and not any(
                model.device_template is not None for model in plugin.models
            ):
                raise SDKValidationError("API2 plugin must provide a resource descriptor or a model device_template")
            if (
                manifest.operations
                and type(plugin).invoke is PrinterPlugin.invoke
                and type(plugin).operation_handlers is PrinterPlugin.operation_handlers
            ):
                raise SDKValidationError("API2 plugin declares operations but supplies no operation handlers")
    except TypeError as exc:
        raise SDKValidationError("Plugin lifecycle or operation signatures do not match the SDK") from exc
    if core_version is not None:
        check_manifest_compatibility(
            manifest, core_version=core_version, supported_api_versions=("1.0.0", SDK_API_VERSION)
        )
    return manifest

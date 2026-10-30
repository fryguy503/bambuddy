# Printer plugin authoring reference

The printer SDK describes hardware and behavior. A plugin translates that
description into a device protocol; core owns persistence, permissions,
queueing, operation tracking and the shared UI. New models, tool counts,
operating limits, actions, configuration fields and images come from plugin
metadata, so compatible plugin updates do not require a frontend build.

The shared UI renders descriptor schemas. Included Bambu compatibility views
live under `frontend/src/printer-plugins/bambu` and are selected by the optional
`extensions.compatibility_ui = "bambu-v1"` descriptor flag. New resources and
operations use the generic renderer; a plugin can omit that compatibility flag.
Package image assets are supported, while arbitrary package-supplied JavaScript
panels are not loaded. A new interaction that cannot be expressed by the
existing schemas may require a host UI change.

The SDK is Python code in `backend/app/printers/sdk`. Generate the exact
machine-readable contracts instead of copying an older example:

```console
python -m backend.app.printers.sdk schema manifest
python -m backend.app.printers.sdk schema device
python -m backend.app.printers.sdk schema snapshot
python -m backend.app.printers.sdk schema request
python -m backend.app.printers.sdk schema receipt
python -m backend.app.printers.sdk schema event
python -m backend.app.printers.sdk schema job-event-data
```

## Compatibility and package identity

An independently installed archive contains a root `manifest.json` and a
`payload/` directory. The manifest declares:

| Field | Meaning |
| --- | --- |
| `manifest_version` | Package manifest format; currently `1` |
| `plugin_id` | Stable lowercase provider identity, such as `example.toolchanger` |
| `name`, `version` | Display name and a `major.minor.patch` plugin release version |
| `api_version`, `requires_api` | SDK major and explicit supported SDK version range |
| `requires_core`, `requires_python` | Explicit compatible host and interpreter ranges |
| `entrypoint` | Payload-relative Python `module:Factory`, for example `plugin:ExamplePlugin` |
| `publisher_id` | Stable publisher identity used to check update continuity |
| `dependencies` | Required installed Python distributions and compatible ranges |
| `files` | Every payload-relative file and its lowercase SHA-256 digest |
| `configuration_schema` | Schema for saved `PrinterConfig.options` |
| `models`, `capabilities`, `operations` | Declared model metadata and optional static operation metadata |
| `extensions` | Namespaced additions understood by the plugin and optional consumers |

The host checks static metadata, compatibility, dependencies and the complete
file inventory before loading package code. It does not run `pip`, resolve
dependencies from the network, or install an application update during package
activation. Package code is trusted application code. A matching checksum
checks integrity; it is not a publisher signature or proof that code is safe.

The included Bambu plugin is the initial bundled provider. A compatible
managed package with the same provider and publisher identity may replace the
bundled version. Existing printer records retain `plugin_id="bambu"`. Updating
printer support therefore does not require a new Bambuddy release.

See [plugin package management](printer-plugins.md) for staging, activation,
restart requirements, disabling, removal and rollback behavior.

## Implementing a plugin

A runnable six-tool development simulator is included in
[`examples/printer_plugins/reference`](../examples/printer_plugins/reference).
It uses no printer hardware or network connection. Its `ReferencePlugin` shows
resource relationships, distinct heater limits, typed operations and job plans.
It is an authoring fixture, not additional production printer support.

Regenerate its fixtures and check them from the repository root:

```console
python -m examples.printer_plugins.reference.generate_fixtures
python -m backend.app.printers.sdk manifest examples/printer_plugins/reference/manifest.json
python -m backend.app.printers.sdk fixtures examples/printer_plugins/reference/device.json --snapshot examples/printer_plugins/reference/snapshot.json --request examples/printer_plugins/reference/request.json
```

For a package archive, put that generated `manifest.json` at the ZIP root and
the inventoried Python files under `payload/`. Do not include generated
conformance fixtures, `__pycache__`, a virtual environment or the application
itself. Regenerate inventory hashes after changing any payload file.

Subclass `PrinterPlugin` from `backend.app.printers.contracts`. Set a stable
`id`, a separate plugin `version`, `api_version = 2`, publisher identity and
model descriptors. Supply the methods below:

| Method | Contract |
| --- | --- |
| `create_connection(config, *, on_event=None)` | Return a managed connection; explicitly opt into typed events without requiring `**kwargs` |
| `describe_device(config)` | Return a validated `DeviceDescriptor` |
| `snapshot_device(config, connection)` | Return a `DeviceSnapshot` with normalized resource values |
| `operation_handlers(connection)` | Map declared operation IDs to handlers accepting one `OperationRequest` |
| `invoke(config, connection, request)` | Default implementation validates, calls the matching handler and validates its result; override only while retaining that contract |
| `operation_status(config, connection, task_id)` | Return current receipt for a plugin-managed asynchronous task, if used |
| `cancel_operation(config, connection, task_id)` | Return a cancellation receipt when the operation advertises cancellation |
| `validate_config(config)` | Validate ownership and the options schema; override to check protocol-specific requirements |
| `can_deactivate(config, connection)` | Refuse replacement while a provider-specific operation makes it unsafe |
| `loaded_materials(state)` | Optional normalized inventory override; default reads stable source/tool IDs from the SDK host projection |
| `status_fingerprint(state)` | Optional protocol state that should trigger a host status broadcast |
| `archive_filament_metadata(data, mapping=None)` | Optional normalized archive material fields when an artifact is unavailable |
| `derive_failure_reason(status, data)` | Optional provider policy for a stable failure code; default accepts normalized `failure_reason` |
| `cleanup_after_print(printer_id, data, archive_id, logger)` | Optional asynchronous provider cleanup; default performs no device file operations |
| `start_host_services(host)`, `stop_host_services(host)` | Optional asynchronous provider-owned background services tied to application startup/shutdown |

A connection provides `connect()`, `disconnect(timeout=0)` and
`check_staleness()`. Core retains the connection; a UI command should not open
another protocol session. Reconnection and transport error handling belong to
the plugin. Secrets live in `config.access_code` or `config.options`, not in a
device descriptor, state snapshot, image URL or operation result.

## Typed device events

Providers with an additional durable plate-clear gate can declare the optional
`job.clear_plate` operation. The shared `/printers/{id}/clear-plate` endpoint
checks its declared permissions and invokes it before releasing the host gate.
It must return a successful receipt; a rejection or unfinished operation retains
the host hold. Providers without this operation retain the ordinary offline
plate-clear acknowledgement. Keep printer-specific confirmation rules inside
the provider operation.

API2 plugins opt in by declaring an optional named `on_event` parameter on
`create_connection`. Core passes only callback names supported by a fixed
signature. Accepting `**callbacks` alone retains the legacy callback contract;
it does not inject a new argument into an existing protocol constructor.

The event sink accepts a `DeviceEvent` instance or its serialized dictionary.
Its fields are `schema_version=2`, `device_id`, monotonic `sequence`, `event`,
timezone-aware `observed_at`, optional `resource_id`/`request_id`, `data` and
namespaced `extensions`. The sink is synchronous and may be called from a
protocol thread. Its `True` result means accepted for host processing, not that
asynchronous archive work has committed.

| Event | `data` | Host behavior |
| --- | --- | --- |
| `device.snapshot`, `device.status` | Serialized `DeviceSnapshot` | Validate identity/revision/resources, update the normalized host status view |
| `resource.telemetry` | Serialized `DeviceSnapshot` | Same snapshot path; a vendor wire fragment or an untyped property patch is rejected |
| `job.started` | Serialized `JobEventData` | Notify the existing print-start/archive workflow with stable job identity |
| `job.progress` | Serialized `JobEventData` | Update the job view and optional percentage/layer callbacks |
| `job.completed`, `job.failed`, `job.cancelled` | Serialized `JobEventData` | Notify the existing completion/archive/queue workflow with the explicit terminal status |

`JobEventData` contains `job: JobProgress` and optional `artifact: JobArtifact`,
`snapshot: DeviceSnapshot`, and structured `error: OperationError`. Active tool
IDs must exist in the device descriptor. A terminal event's job state must
agree with that terminal status. If no snapshot accompanies a job event, core
updates the normalized job view while retaining prior resource telemetry;
unknown resource values remain unknown.

For example, after updating its own normalized state, a six-tool driver can
publish its start without constructing a Bambu wire message:

```python
on_event(DeviceEvent(
    device_id=descriptor.device_id,
    sequence=next_sequence,
    event="job.started",
    data=JobEventData(job=state.job, artifact=artifact,
                      snapshot=state).model_dump(mode="json"),
))
```

Core maps job identity to its existing `subtask_id`, artifact filename to
`filename`, job display name to `subtask_name`, and the explicit event to
`status`. It retains typed `sdk_job`, `sdk_event` and optional artifact/error
metadata. Plugins do not construct `raw_data`, AMS tray mappings or HMS errors.

Sequences are scoped to a connection. Duplicate/older sequences and repeated
start/terminal notifications for recent job IDs are ignored; snapshots cannot
move their sequence backwards. Preserve job identity across reconnects and
distinguish a new print from observing an already running one. Unknown custom
event names have no implicit archive semantics. Legacy callbacks remain
supported for the included compatibility provider.

## Device topology

`DeviceDescriptor` contains `schema_version=2`, `device_id`, optional model/name,
`revision`, capabilities, resources, operations, configuration schema and
extensions. Increment `revision` when the device description changes.

There is no fixed number of tools or positional left/right convention.
`tool:bay-7` is an opaque identity; array position `7` is not a hardware address.
Keep resource IDs stable across reconnects and compatible package updates.

Each `ResourceDescriptor` contains:

- `id`, `kind`, `label` and optional `parent_id`.
- `roles` and capabilities describing supported behavior.
- `relations`, mapping relation names to resource IDs.
- `properties`, mapping property names to typed property descriptors.
- `metadata` and namespaced `extensions` for descriptive additions.

Common resource kinds include `tool`, `hotend`, `extruder`, `heater`,
`material_source`, `material_slot`, `material_router`, `dock`, `axis`, `fan`,
`sensor`, `camera`, `bed`, `chamber`, `light`, `storage`, `power`, `probe`,
`filtration` and `motion_system`. New kinds such as `example:attachment` can be
described without adding an enumeration member to core.

Model physical components separately. A tool may reference several hotends or
extruders; several tools may share a heater, material router or motion system.
Explicit `hotends`, `extruders`, feeding and docking relations carry that
information. Do not infer the relation from resource array order, numeric
suffixes or a vendor's command IDs. Parent IDs and relation targets must exist;
parent relationships must not contain cycles.

## Properties, units and limits

A `ResourceProperty` declares `value_schema`, optional label/description/unit,
read/write support and read/write permissions. A writable property may identify
its `operation_id` and the operation `parameter` that receives the requested
value. Limits belong to the resource, rather than a global printer-family cap.

For example, one tool heater can permit `0–300 °C` and another `0–520 °C`.
A fan may accept a percentage, RPM or a named mode. An axis may expose a
different travel range and unit. Declare actual ranges, enumeration values and
units instead of translating them to Bambu's modes or PWM values in core.

Schemas use self-contained JSON Schema Draft 2020-12. External `$ref` URLs and
schema `$id` scopes are rejected; use local `$defs` references. Unknown schema
shapes remain representable through the UI's JSON input. Server validation is
authoritative even when the browser offers a specialized control.

## Operations

An `OperationDescriptor` declares:

| Field | Meaning |
| --- | --- |
| `id`, `namespace`, `label`, `description` | Stable operation identity and display metadata |
| `resource_ids`, `resource_kinds` | Resources on which the operation can run; empty lists describe a device-level action |
| `parameters_schema`, `result_schema` | Accepted parameters and successful result data |
| `resource_parameters_schemas` | Optional per-resource narrowing of parameter constraints |
| `permission` | Required existing host permission; the host also enforces printer scope |
| `asynchronous`, `cancellable` | Task and cancellation support |
| `idempotent`, `timeout_seconds` | Retry semantics and operation budget |
| `confirmation` | Optional explicit user confirmation text |

Use standardized IDs for standardized behavior, such as `tool.select`,
`heater.set_target`, `fan.set_speed`, `motion.home` and `job.start`. Vendor-only
functions use a stable namespaced ID. A declared operation must have an
implementation; declaring a capability does not implement it.

The handler receives an `OperationRequest` containing `operation_id`, optional
`resource_id`, `parameters`, `request_id` and optional `idempotency_key`.
Resource IDs are kept separate from typed parameters. Validate resource kind,
relationships and ranges before sending a hardware command. Do not return
success merely because a socket write completed when the operation promises a
hardware acknowledgement.

Receipts contain the operation/request IDs, `status`, optional task/result/error
and progress, plus timezone-aware accepted/updated timestamps. Statuses are
`accepted`, `running`, `succeeded`, `failed` and `cancelled`. In-progress results
require a task ID. Failed results require an error with a stable `code`, message,
retryability and optional structured details. Progress may provide fraction or
completed/total/unit values. Cancellation is valid only for operations declared
as asynchronous and cancellable. A cancellation request is not proof the
physical device has stopped; report the confirmed state.

## Snapshots and freshness

`DeviceSnapshot` carries device identity, descriptor revision, sequence,
observation timestamp, connection/status, optional job identity and a list of
`ResourceState` values. A state names `resource_id`, optional availability/status
and property readings. Each `TelemetryValue` contains a JSON value, optional
unit, observation time, optional validity deadline, quality and source.

Quality is one of `known`, `unknown`, `stale`, `unavailable` and `error`. Unknown
does not mean zero; disconnected does not mean every last reading is current.
The UI displays stale/unknown quality instead of treating an old value as a
fresh measurement. State resource IDs must match the descriptor.

## Job plans and material routing

`JobArtifact` describes the server-authorized uploaded file, format, optional
media type, filename, byte count, SHA-256, storage resource and metadata.
`JobRequest` contains job identity, artifact, optional name, material bindings,
selected tool IDs, optional build surface/plate IDs, plugin options and
extensions.

Each `MaterialBinding` identifies an artifact `input_id` and a physical
`tool_id`, with optional source, slot, hotend and extruder IDs and material usage
requirements. Thus a shared router feeding five tools can be represented
without a two-extruder offset or tray-ID arithmetic. Referenced resources and
declared topology must agree. Ambiguous multi-tool mappings are rejected rather
than assigned to the first tool.

Queue requests can carry a complete `sdk_job`. The convenience fields
`tool_ids`, `material_bindings` and `job_options` support simple plans; a complete
plan is authoritative when material routing requires explicit tool relations.
Explicit resource plans require a concrete `printer_id`, because resource IDs
belong to that physical device. Queue create/update validates the plan against
its `job.start` schema and topology before saving. Reassigning the target clears
the previous resource plan unless valid replacements are supplied.
For a queued dispatch, core assigns the authoritative job identity (currently
`queue:<queue-item-id>`). Echo the supplied identity in progress and lifecycle
events; do not reconstruct it from the filename or display name. Device-originated
jobs may use their own stable SDK job IDs, up to 256 characters.
The host resolves `library:<id>` or `archive:<id>` against authorized stored
files and substitutes the actual uploaded artifact URI. Submit print jobs
through the normal queue workflow so permissions, budget, plate-clear gates
and host checks are retained.

## HTTP access for integrations

| Route | Result |
| --- | --- |
| `GET /api/v1/printer-plugins` | Running plugin/model/configuration descriptors |
| `GET /api/v1/printers/{id}/device` | Device topology and typed operations |
| `GET /api/v1/printers/{id}/device-state` | Normalized snapshot |
| `POST /api/v1/printers/{id}/operations/{operation_id}` | Invoke an allowed scoped operation |
| `GET /api/v1/printers/{id}/operation-tasks/{task_id}` | Task receipt/status |
| `POST /api/v1/printers/{id}/operation-tasks/{task_id}/cancel` | Request supported cancellation |
| `POST /api/v1/queue/` | Host-managed job creation with optional `sdk_job` |

An operation body is `{ "resource_id": "heater:tool-7", "parameters":
{ "target": 450 }, "request_id": "542a1e30-3679-4c66-8dfa-343d624352bc" }`. The resource's declared
schema must permit that value. Authentication and the descriptor's permission
are enforced by the host. Hardware job start remains behind the queue workflow;
the generic command endpoint does not bypass its gates.

## File, camera and optional services

Use `file_operation_for(config, operation, ...)` and
`camera_operation_for(config, operation, ...)` to access persisted options even
for an inactive printer. Core owns caches and retry policy. Network/format
translation belongs in the plugin; no printer is required to use FTP.

Storage rules default to normalized `state.storage` metadata and can be
overridden. Discovery, connection diagnostics, firmware, cloud access,
connection recovery and slicer-facing virtual-printer emulation are optional
services. Unsupported services return the controlled unsupported-operation
response rather than silently using another vendor's implementation.

Live camera streams provide JPEG frames or complete multipart MJPEG chunks;
core owns stream buffering, stop requests and cleanup. Package image assets
are served through `/api/v1/printer-plugins/{plugin_id}/assets/{path}`. Include
assets in the manifest inventory and use those URLs in model metadata instead
of relying on files compiled into the application.

## Author validation and release checklist

Validate metadata without importing a printer protocol or contacting hardware:

```console
python -m backend.app.printers.sdk manifest manifest.json
python -m backend.app.printers.sdk fixtures device.json --snapshot state.json --request request.json --receipt receipt.json
```

In development tests, `validate_plugin_contract(plugin)` checks the executable
contract; `validate_invocation`, `validate_snapshot` and `validate_receipt` test
actual fixtures. Include at least these cases:

1. Zero/unknown optional components and more than two tools; IDs reordered
   without changing their physical meaning.
2. Per-resource limits, invalid enums/types, unavailable/stale readings and
   missing/invalid topology references.
3. Successful, rejected, timed-out, disconnected and cancelled operations;
   duplicate requests and idempotency behavior where supported.
4. Active-print reconnect, job identity, material routing and host event order.
5. Offline configuration, secret redaction and missing optional services.
6. Compatible upgrade, incompatible manifest/dependencies, rejected payload
   inventory, failed load and rollback to the last working version.

These checks establish API conformance, not physical hardware certification.
Test supported models, firmware versions, transports, accessories and recovery
behavior on the devices before advertising those combinations as supported.

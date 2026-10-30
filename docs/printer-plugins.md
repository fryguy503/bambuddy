# Printer plugins

Printer support is provided by installed, trusted Python plugins. Bambu Lab
support ships as the included `bambu` plugin. This branch starts from upstream
`dev` and does not include the earlier Anycubic implementation.

For the complete version 2 authoring contract, examples and schema commands,
read the [printer plugin authoring reference](printer-plugin-authoring.md).
For installation, independent Bambu updates, signatures and rollback, read the
[plugin management guide](printer-plugin-management.md). The compatibility
helpers below preserve existing application integrations alongside that SDK.

## Ownership

The core application owns printer records, permissions, queueing, archives,
notifications, shared file caches and retry policy, and the common printer state.
A printer record stores its
`plugin_id`; existing records migrate to `bambu` automatically. An explicitly
configured plugin that is missing is an error, rather than a reason to connect
using a different vendor's protocol.

`backend/app/printers/contracts.py` retains the API version 1 compatibility
interface and exposes version 2 hooks; `backend/app/printers/sdk` defines the
version 2 resource, operation, snapshot and manifest schemas:

- `PrinterConfig` carries the printer's identity, connection settings and plugin
  identity. Access credentials are excluded from its representation.
- `PrinterConnection` defines `connect()`, `disconnect()` and `check_staleness()`.
  Status refresh is an optional declared command. API2 connections publish
  `DeviceSnapshot` and typed events through core callbacks. The host projects
  snapshots into its workflow view without constructing Bambu state classes.
- `PrinterModel` describes a model, aliases, image, capabilities and optional
  G-code compatibility group.
- `PrinterPlugin` creates connections and file clients, exposes supported
  resource operations, and adapts file, camera, discovery, firmware, cloud and diagnostic
  operations. Optional operations raise `UnsupportedPrinterOperation` until a
  plugin implements them.

The included implementation lives in `backend/app/printers/bambu/`. It owns
Bambu MQTT commands and parsing, FTP transport, printer camera protocols,
SSDP/subnet discovery, model and firmware rules, cloud access and HMS data.
It also owns legacy AMS inventory parsing, failure-code interpretation,
calibration routing, and status-change policies. Included compatibility UI
components live under `frontend/src/printer-plugins/bambu`; new models and
features use server descriptors and the shared schema renderer.
Its `virtual_printer/` package also owns Bambu-compatible slicer emulation,
including MQTT, FTP, discovery, pairing, certificates and proxy behavior.
Old service import paths remain compatibility adapters so existing callers
and integrations can migrate without changing their public imports.

## Internal addon API

Plugins and core code use the in-process API:

```python
from backend.app.printers import get_plugin
from backend.app.printers.api import printer_api
from backend.app.printers.sdk import OperationRequest

plugin = get_plugin(printer.plugin_id)
description = plugin.descriptor()
capabilities = printer_api.capabilities(printer)

# Core supplies event callbacks when creating a managed connection.
connection = printer_api.create_connection(printer, on_state_change=on_state_change)
device = printer_api.describe_device(printer)
snapshot = printer_api.snapshot_device(printer, connection)
receipt = await printer_api.invoke(
    printer, connection, OperationRequest(operation_id="job.pause")
)
```

Use the printer manager's existing managed connection when issuing commands
for an active printer; do not create a second connection for each command.
API2 operations are described by `DeviceDescriptor.operations`, with schemas,
resource selectors, permissions, limits, and structured results. API1 method
commands remain available through `execute()` and must appear in the plugin's
`operations` list and `command_handlers()` mapping. Their arguments are
validated against the handler's signature before invocation.

Shared file caches and retry policy belong to core. Plugins do not need to
implement another cache to participate in queueing, archives or deletion.
`file_operation_for()` and `camera_operation_for()` receive the saved
`PrinterConfig`, including secret `plugin_options`, even when a printer is
inactive and no connection has been created. Ordinary printer responses and
plugin metadata omit these options.

`printer_api.files()` and `printer_api.camera()` select the adapter using the
printer record and call `file_operation_for(config, operation, ...)` or
`camera_operation_for(config, operation, ...)`. These hooks receive the full
persisted `PrinterConfig`, including `config.options`, even if the printer is
inactive and no status connection has ever been created. A plugin can read its
file or camera authentication settings directly from that config. Existing
adapters may keep implementing `file_operation()` and `camera_operation()`;
the default config hooks validate ownership and delegate to those methods
without adding arguments to the underlying transport functions.

File and camera facades preserve the existing async/sync calling convention
for each operation. Return an awaitable for async operations and an immediate
value for synchronous operations. Core owns shared file caching and retry
policy, so plugins do not need to implement another cache or retry loop.

The same internal facade exposes `serialize_state(printer, state)`,
`discovery(plugin_id, operation)`, `firmware(printer, operation)`,
`diagnostic(printer, operation)` and `cloud(plugin_id, operation)`.

## Shared state and telemetry

API2 plugins populate `DeviceSnapshot` using the stable IDs in their device
descriptor. Tools, hotends, heaters, docks, sensors, material sources and future
resource kinds have no fixed count. Telemetry includes units, quality and
freshness; tool routing uses explicit `JobRequest` bindings. The legacy
`PrinterState` import remains a deprecated Bambu compatibility export.

`derive_status(state, model)` supplies the status label;
`serialize_state(state, printer_id, model, drying_targets)` supplies the common
REST and WebSocket snapshot. Their defaults use normalized state and ignore
vendor `raw_data`. Override them when the hardware requires translation. The
Bambu serializer owns Bambu stage codes, AMS decoding and tray parsing.

`normalize_telemetry(state)` supplies the normalized filament-unit, external
spool and nozzle mapping data used by queueing and other core workflows.
`storage_operation_for(config, operation, ...)` provides configured storage
rules; its default delegates to generic normalized storage handling. Hardware
specific storage schemes and flags belong in the plugin. Optional
`recover_connection(config, connection, offline_grace)` handles transport
recovery without putting vendor watchdog rules into core.

`get_virtual_printer_service(plugin_id)` in `printers/virtual_printer.py` selects
the plugin's optional `virtual_printer_service()` hook. The included Bambu
plugin returns its existing virtual-printer manager singleton. Historical
`services.virtual_printer` package and submodule imports alias that same bundled
implementation, preserving integrations and module patch paths.

## HTTP access

The authenticated HTTP interface exposes plugin metadata:

| Route | Result |
| --- | --- |
| `GET /api/v1/printer-plugins` | Installed plugin descriptors and model catalogs |
| `GET /api/v1/printer-plugins/{plugin_id}` | One installed plugin's descriptor |
| `GET /api/v1/printer-plugins/printers/{printer_id}` | A printer's plugin, model capabilities and commands |

Metadata requires the existing `printers:read` permission; printer-specific
metadata also applies the normal printer scope. Existing scoped
`/api/v1/printers/{printer_id}/...` routes remain the HTTP entry points for
commands, state, files and camera access. They retain their existing permission
checks while dispatching through the selected adapter. There is no separate
raw-command HTTP route that bypasses those checks.

Printer creation accepts `plugin_id` alongside the existing fields:

```json
{
  "plugin_id": "bambu",
  "name": "Workshop",
  "ip_address": "192.168.1.100",
  "serial_number": "PRINTER_SERIAL",
  "access_code": "PRINTER_ACCESS_CODE",
  "model": "X1C"
}
```

Discovery results identify their `plugin_id`. Setup diagnostics accept the
same identity, so a plugin's printer is never probed using Bambu's transport
just because it shares the same address or model label.

## Adding printer support

Implement `PrinterPlugin` in a separate Python package and publish its factory
using the `bambuddy.printers` entry point group:

```toml
[project.entry-points."bambuddy.printers"]
example = "example_printer.plugin:ExamplePrinterPlugin"
```

The factory returns a `PrinterPlugin` subclass with a unique lowercase `id`
(letters, digits, underscores, dots and hyphens), model descriptors, declared
operations, and `create_connection()`. New integrations use `api_version = 2`
and the resource SDK; the version 1 bridge remains for existing adapters. Implement the
optional transport operations supported by the hardware. This Python entry-point
mechanism remains available for host-managed distributions. Prefer a versioned
managed package for administrator installation, independent upgrades and
rollback. Uploaded packages are inspected without importing them; explicitly
selected compatible code refreshes printer connections immediately without
stopping their current prints. Older versions remain available for already
accepted work and its completion callbacks. Installed plugins run
as trusted application code, not in a sandbox for untrusted scripts.

The registry loads bundled support and installed entry points. Duplicate IDs,
unsupported API versions and invalid command/model descriptors are rejected.
No additional printer implementation is included in this change.

## Frontend model metadata

The Add and Edit Printer dialogs load their model options from the installed
plugins. When more than one plugin is installed, the Add dialog lets the user
choose its printer support. A saved model that a plugin no longer advertises
remains selectable in the Edit dialog.

Discovery aliases, printer images and hardware-specific card controls resolve
within the owning plugin, allowing vendors to reuse model names. Model
capabilities include `door_sensor`, `chamber_fan`, `exhaust_fan`, `airduct`,
`aux_fan_left` and `bed_slinger`. The bundled Bambu frontend metadata lives in
`frontend/src/printer-plugins/bambu/` and is used while the server catalog loads;
the installed descriptor replaces it. New models supported by a plugin appear
without adding another core model dropdown or hardware feature list.

Plugins translate hardware into the shared state and operation contract.
Version 2 describes arbitrary resource collections and operations, including
tool counts, heaters, fans, sensors, axes and vendor extensions through metadata.
Changing the contract's meaning still requires a new API version; merely
declaring a capability does not implement the hardware behavior.

## Storage and live cameras

`storage_operation()` can customize file reachability and probe paths. Its
default reads only normalized `PrinterState.storage`: `reachable`,
`external_present`, `reason`, `probe_filename` and `probe_paths`. Unknown
reachability remains worth attempting; an explicit `false` skips the operation.
Bambu URL schemes, removable-media flags and FTP directories are interpreted
inside the bundled plugin.

For live cameras, `camera_operation_for(config, "stream_fps", model, fps)`
selects a rate, and the `"stream"` operation supplies an async iterator of JPEG
frames or complete multipart MJPEG chunks. Core owns buffering, stream status,
stop requests and cleanup. The bundled plugin owns the Bambu camera protocols.

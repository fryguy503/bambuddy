# Anycubic printer plugin

The independent Anycubic provider uses Bambuddy's printer SDK v2. Its current
release is `anycubic` **0.1.5**, published under `fryguy503`. The included Bambu
provider remains a separate plugin.

This package requires the printer plugin implementation from
`Feat/Printer_modularization` or `codex/printer-modularization-https`. An upstream
application without the SDK v2 package manager cannot install it; the
application version string alone does not establish SDK availability.
Version 0.1.5 requires SDK 2.3; update the host before uploading it. Earlier
SDK hosts reject this package during compatibility validation.

Version 0.1.5 adds provider-owned LAN connection diagnostics and corrects the
Kobra S1 capability description: it does not expose a chamber temperature
sensor or heater. Other models expose a chamber reading only when supported
and actually reported; no chamber heater command was added.

The large-file optimization remains: it reduces CPU work while retaining both
complete scans, content hashes and changed-file checks. Five concurrent
100 MB synthetic files took 16.8 seconds in the local benchmark, compared with
33.5 seconds before this change; NAS timings depend on CPU and storage.

The package supplies file-specific print options and ACE input requirements
through the SDK job-description endpoint. Shared batch choices map back to each
file's original sliced inputs. It restores familiar control editors through
declarative presentation hints, labels ACE units from 1, and advertises native
G-code upload. Core workflows remain provider-neutral.

The 0.1.2 durability behavior remains: finalized dispatch outcomes stay authoritative when overlapping
older observers report stale progress. It preserves pending accounting and
repairs legacy journal fields under the existing storage lock. A terminal
observation awaiting host cleanup remains replayable; no print command is
replayed. The published 0.1.0 through 0.1.4 archives are unchanged.

## Install or update

The release artifact is
[`packages/anycubic/anycubic-0.1.5.bbp`](../../packages/anycubic/anycubic-0.1.5.bbp).
Its adjacent `.sha256` file records the archive's SHA-256 digest.

1. In **Settings → Printer plugins** (`/settings?tab=printer-plugins`), upload the
   `.bbp` file and optionally paste its expected SHA-256 digest.
2. Review the validation result, then apply version `0.1.5`. Staging alone does
   not load or enable its code.
3. Add or edit a printer, select **Anycubic**, and choose its model.

Package updates and rollbacks refresh data connections immediately, including
while printers are printing or paused. They do not wait for idle printers,
drain queues or require an application restart. Existing accepted operations
and current-print callbacks keep their original provider until finished;
refreshing never resends print commands. The plugin manager shows the selected
default, versions still in use and each printer's refresh result. Install the
host's live-update implementation once before using this flow on an older host.
Existing application data and plugin selections belong in your normal
pre-upgrade backup.

The archive contains trusted Python code. Its checksum verifies integrity; the
publisher label and checksum do not constitute a cryptographic publisher
signature. The host checks its manifest, supported versions, dependencies,
payload inventory and executable contract. Hardware behavior still needs
verification on the supported printer and firmware combinations.

The standalone payload includes the repository's AGPLv3 license text and
declares `AGPL-3.0-only`. Its dependency ranges cover the installed `aiohttp`,
`cryptography`, `httpx` and `paho-mqtt` distributions; installation checks these
versions without downloading or installing dependencies.

## Connection diagnostics

The host's connection check uses this provider's protocol checks. It tests the
HTTP identity service on port 18910, supported model and printer identity,
MQTT TCP/TLS on port 9883, the approved certificate fingerprint, authenticated
subscription and fresh telemetry. File readiness checks the advertised LAN
upload endpoint; it does not upload a test file. The optional camera check
only tests TCP port 18088 and never starts or verifies a video stream.

A saved printer reuses its live connection without reconnecting, pairing again
or issuing printer commands. An unsaved check may establish an isolated LAN
pairing/subscription only after its identity and approved fingerprint match.
It sends status queries and closes its temporary connection after the check,
including cancellation. A missing fingerprint skips authentication and asks for
review; a mismatch fails without attempting authentication. Public results do
not include credentials, handshake tokens, upload URLs or exception details.

TCP connection and TLS handshake probes use 4-second deadlines, with up to
1 extra second for socket closure. HTTP info has an 8-second total deadline.
Temporary telemetry observation defaults to 8 seconds and is capped at 15;
cleanup can wait up to 30 seconds for the native pairing worker to finish.
Ports alone do not establish that authentication, telemetry or a camera frame
works. Host/container/subnet guidance is supplied by the shared SDK.

ACE is optional by default. The package's `require_ace` configuration option
can require an attached unit: absent optional ACE is skipped, a missing required
unit fails, and detected but stale material information produces a warning.

## LAN connection

The model catalog includes Kobra S1, Kobra S1 Max, Kobra 3, Kobra 3 V2,
Kobra 3 Max, Kobra 4 and Kobra X. Advertised controls and their limits come from
the plugin's device descriptors and the printer's reported capabilities.

Use the printer's private LAN IP and discovered printer identity (the host's
`serial_number` connection field), plus an access code if required. Configure the
required `tls_fingerprint` option with the approved SHA-256 certificate
fingerprint for that printer. Connection setup requires that pin and does not
silently trust a different certificate. Verify the fingerprint through your
existing trusted pairing procedure before saving it.

The plugin owns the Anycubic protocol, resources, material addressing, file and
camera services. Shared permissions, queue admission, budgets and application
workflows remain in Bambuddy. New model metadata and schema-described controls
can be released as plugin updates without rebuilding the shared web UI.

Print artifacts must be sliced text `.gcode` exports with matching printer
model and filament metadata. Material input IDs refer to the original sliced
`T` inputs; the physical tool remains `tool:primary`, while ACE resources name
the selected box and slot separately.

The per-job `allow_material_type_mismatch` option defaults to `false`. Turning
it on permits an explicitly selected, known loaded ACE material with a different
type from the sliced input. It does not allow empty, unavailable, unknown or
stale slots, change resource bindings, or bypass the final native preflight.
The choice is retained in the queued SDK plan and durable dispatch record.
The native mapping keeps the original sliced material type; no new firmware
setting is invented for this host-side policy choice.

This option does **not** change temperatures, cooling, flow, retraction or the
toolpath. The reviewed native `task_settings` contract has no temperature-profile
override fields, and later G-code commands can overwrite a one-time temperature
setter. Reslice the file when a different temperature or material profile is
required; a partial temperature override is not offered.

The provider's durable plate-clear hold is exposed in the normalized snapshot
as `plate_clear_required`. It survives reconnects. A successful real plate-clear
acknowledgment updates the journal and publishes the cleared snapshot immediately;
fresh idle state and unresolved-dispatch checks still apply.
The descriptor declares `plate_clear_policy: "required"`, so the host also keeps
a hold raised by an older workflow owner during a rolling plugin refresh, even
before the new observer's cached snapshot reflects it. This requires no journal
read for each displayed snapshot.

An accepted operation receipt means a command was queued or its native receipt
was observed. It does not by itself confirm physical execution. Matching fresh
telemetry confirms supported controls through task polling. Print dispatch
intent is persisted before sending. An uncertain outcome survives reconnects
and is not automatically replayed; inspect the printer, use the explicit
resolve operation while it is freshly idle, then confirm the plate is clear
before a new dispatch. Resolving a hold does not reuse the previous job ID.

Package-owned job/artifact/inventory state is stored under
`DATA_DIR/printer-plugin-state/anycubic`; installed package versions and their
selection are under `DATA_DIR/printer-plugins`. Preserve both across container
updates and include the full application data volume and encryption keys in
backups. Do not remove the journal to bypass an unresolved dispatch.

When automatic archiving is disabled and a completion has no attached archive,
filament-consumption accounting remains deferred until a matching archive is
attached. Hardware completion is tracked separately: after a confirmed print
finishes, normal plate-clear allows the next dispatch even while accounting is
deferred. Accounting-only records retry on reconnect without replaying printer
commands or lifecycle events. The plugin does not guess an archive or spool to charge. Automatic
spool accounting currently uses built-in inventory; it is skipped when Spoolman
owns assignments so the two systems cannot book the same consumption.

## Optional cloud metadata

LAN printing does not require a cloud account. The package also supplies these
host-authenticated routes under `/api/v1/printer-plugins/anycubic/cloud`:

| Route | Purpose |
| --- | --- |
| `GET /status` | Read local connection status without contacting cloud services |
| `POST /token` | Validate a supplied web token or Slicer Next SSO access token; JSON fields `token` and `region` (`global` or `china`) |
| `POST /logout` | Remove this user's stored cloud connection |
| `GET /account` | Read safe account identity |
| `GET /profiles`, `GET /profiles/{setting_id}` | Read cloud presets with credential fields masked |
| `POST /catalog/materials` | Read the native material catalog using supported filters |
| `GET /models/{numeric_model_id}` | Read global MakerOnline metadata; no file download or library write |

Host cloud-auth permission controls connection/logout; printer-read,
filament-read or own-library-read permissions protect the relevant metadata.
Owned API keys must additionally have cloud access enabled. Verified credentials
are encrypted in owner-specific host Settings rows. The API refuses to save a
token when encryption is unavailable, and an earlier login cannot overwrite a
newer logout. These services do not open cloud MQTT or send cloud printer
commands.

## Build a release

From this repository root, using the application's Python environment:

```console
python plugins/anycubic/build.py
```

The builder writes `plugins/anycubic/manifest.json`, the versioned package under
`packages/anycubic/`, and its checksum file. It imports only the release payload
to validate its contract and descriptors; it does not connect to a printer.
The package contains a root manifest plus the inventoried `payload/` files,
including model images and `LICENSE`. Python, JSON and license text use canonical LF bytes, and ZIP
ordering, timestamps and file modes are fixed for reproducible builds.

For an alternate output location:

```console
python plugins/anycubic/build.py --output work/anycubic-0.1.5.bbp
```

For a later release, update the version in the payload's plugin factory, then
rebuild. Regenerate the package after changing any payload file; manually
editing an installed payload invalidates its inventory.

Run the package tests and offline manifest check with:

```console
python -m pytest backend/tests/unit/test_anycubic_printer_plugin.py
python -m backend.app.printers.sdk manifest plugins/anycubic/manifest.json
```

The tests install the real package into the host registry and exercise SDK
device, operation and job contracts without contacting printer hardware.
See the [SDK authoring guide](../../docs/printer-plugin-authoring.md) for the
complete resource, operation, event, job and configuration contracts, and the
[management guide](../../docs/printer-plugin-management.md) for package trust
and activation behavior.

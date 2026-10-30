# Anycubic printer plugin

The independent Anycubic provider uses Bambuddy's printer SDK v2. Its current
release is `anycubic` **0.1.1**, published under `fryguy503`. The included Bambu
provider remains a separate plugin.

This package requires the printer plugin implementation from
`Feat/Printer_modularization` or `codex/printer-modularization-https`. An upstream
application without the SDK v2 package manager cannot install it; the
application version string alone does not establish SDK availability.
Version 0.1.1 requires SDK 2.1; update the host before uploading it. Existing
SDK 2.0 hosts reject this package during compatibility validation.

## Install or update

The release artifact is
[`packages/anycubic/anycubic-0.1.1.bbp`](../../packages/anycubic/anycubic-0.1.1.bbp).
Its adjacent `.sha256` file records the archive's SHA-256 digest.

1. In **Settings → Printer plugins** (`/settings?tab=printer-plugins`), upload the
   `.bbp` file and optionally paste its expected SHA-256 digest.
2. Review the validation result, then activate version `0.1.1`.
3. Restart Bambuddy to load the selected package. Staging alone does not load
   or enable its code.
4. Add or edit a printer, select **Anycubic**, and choose its model.

Package updates use the same flow. Updating a loaded plugin requires affected
printers and queue jobs to be idle. First activation can restore a missing
provider with no managed connections despite old queue records; it preserves
those records and still checks live printing, uploads and dispatches.
Installed versions can be selected or rolled back through
the plugin manager. Existing application data and plugin selections belong in
your normal pre-upgrade backup.

The archive contains trusted Python code. Its checksum verifies integrity; the
publisher label and checksum do not constitute a cryptographic publisher
signature. The host checks its manifest, supported versions, dependencies,
payload inventory and executable contract. Hardware behavior still needs
verification on the supported printer and firmware combinations.

The standalone payload includes the repository's AGPLv3 license text and
declares `AGPL-3.0-only`. Its dependency ranges cover the installed `aiohttp`,
`cryptography`, `httpx` and `paho-mqtt` distributions; installation checks these
versions without downloading or installing dependencies.

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
python plugins/anycubic/build.py --output work/anycubic-0.1.1.bbp
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

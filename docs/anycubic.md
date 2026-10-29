# Anycubic Kobra LAN support (experimental)

This provider implements stock-firmware LAN monitoring and basic controls for
the signed-protocol Kobra models below. Read-only physical checks passed on one
S1 with two ACE units and eight distinct slots. The other models have offline
protocol/API tests; physical monitoring and all print controls still need
acceptance on each model and firmware.

| Model | LAN model ID | Validation in this project |
| --- | --- | --- |
| Kobra S1 | 20025 | Read-only hardware check, two ACE units |
| Kobra S1 Max | 20029 | Offline protocol and routing tests |
| Kobra 3 | 20024 | Offline protocol and routing tests |
| Kobra 3 V2 | 20027 | Offline protocol and routing tests |
| Kobra 3 Max | 20026 | Offline protocol and routing tests |
| Kobra 4 | 20028 | Offline protocol and routing tests |
| Kobra X | 20030 | Offline protocol and routing tests |

IDs and the shared signed handshake are corroborated by the
[LAN protocol research](https://github.com/chrisfore/anycubic_ha_local/blob/main/research/PROTOCOL-VALIDATED.md)
and [model table](https://github.com/chrisfore/anycubic_ha_local/blob/main/custom_components/anycubic/const.py).
Models using older unsigned protocols, including the Kobra 2 series, are not
admitted by this provider. Unrecognized newer IDs are rejected rather than
being treated as an S1. Combo bundles use the underlying printer model.

| Feature | Initial implementation |
| --- | --- |
| LAN pairing and connection | Signed bootstrap and explicitly reviewed TLS certificate |
| Printer status | State, temperatures, progress, layers and remaining time |
| ACE inventory | Model-specific unit/slot IDs, filament hover menus, inventory assignment and friendly unit names |
| Print controls | Pause/resume/stop, bounded nozzle/bed targets, reported fans and three native speed modes; MQTT delivery is not printer acknowledgement |
| Built-in LAN camera | Camera-only activation, HTTP-FLV decoding, shared browser MJPEG and snapshots |
| External camera | Existing configured external camera support |
| LAN files | Native catalog and model-matched sliced `.gcode` upload, progress and catalog confirmation; never starts printing |
| Queue dispatch and print submission | Model-matched raw G-code, native ACE mapping and capability-gated settings; physical acceptance pending |
| Print history | Raw G-code snapshot and matched native job outcome; no inferred Bambu filament accounting |
| ACE drying | Per-unit temperature, hours/minutes, reported remaining time and stop |
| ACE material controls | Native material/color edits for writable slots; load/unload/stop while idle |
| Manual controls | Reported chamber light/fans, idle XYZ jog/home, capability-gated short E pulses |
| RFID reread, Bambu K profiles, standalone Bambu calibration and firmware | Unavailable in this provider |
| Anycubic Cloud Profiles / MakerOnline | Optional shared account for profile exports and model imports; see [Cloud setup](anycubic-cloud.md) |

LAN monitoring and control do not require an Anycubic account. The optional
Cloud Profiles tab uses a separate token connection to browse and export
slicer presets; it never sends cloud printer commands.

## Adding a printer

1. Enable LAN mode on the printer and give Bambuddy access to its local network.
2. Add a printer and select **Anycubic**.
3. Use **Scan Subnet** to find LAN-mode printers on the selected subnet, or
   enter the IP directly. Detected subnets use OS interface addresses and actual
   netmasks, including secondary addresses, without a fixed LAN range. The
   Synology profile uses host networking to expose the NAS interfaces;
   bridge-mode containers can scan a manually selected LAN subnet.
   Select a result to fill in its address, actual model and identity.
4. Select **Discover Anycubic printer**, review the displayed identity and
   SHA-256 certificate fingerprint, and explicitly trust it before saving.
5. Use a descriptive name. Discovery selects the actual printer model and its
   matching icon. Review that model before saving. `Anycubic` prefixes, `Combo`
   suffixes and the `KS1`/`KS1 Max` aliases normalize to canonical names.

Discovery returns the firmware's `cn` identity where available, or its reported
`usn` on original firmware. It does not invent a Bambu serial or access code.
The saved certificate digest is public and can be read through the printer API.
Broker passwords, bootstrap tokens and client private keys are session-private.

The LAN scanner uses read-only HTTP requests on both native and Docker installs.
Choose an RFC1918 IPv4 subnet containing at most 1024 addresses (`/22` or longer
prefix); a `/24` is typical. It only lists supported models already in LAN mode,
does not pair or trust certificates, and keeps Bambu results separate. Closing
the dialog stops its scan. Existing printers are omitted from the result list.
The existing discovery API accepts `printer_type=anycubic` in the scan request
body and on the result/status/stop query parameters; omission retains Bambu
behavior. All discovery endpoints retain the `discovery:scan` permission.

## Settings, ACE fill and history

With fresh idle/printing/paused status, nozzle and bed popovers send the native
temperature operation for that state. Model limits and any lower reported
firmware limits apply on both the card and API. Fans appear after the printer
reports them; percentages are not converted to Bambu PWM. A reported fan with
an unknown scale remains visible but cannot
be adjusted until its scale is known. Speed offers **Quiet / Standard / Sport**,
using the native idle-mode setting or current-print update. Stale or unknown
capabilities show a waiting state instead of a permission error. Controls
retain the existing printer-control permission and scope. These settings have
offline payload tests; physical heater, fan and speed acceptance remains
pending. No physical setting was changed in testing.

Native command requests use Bambuddy's temporary toast notifications. A queued
toast confirms submission, while subsequent printer telemetry updates the
displayed state. Speed modes retain the native values 1 (Quiet), 2 (Standard)
and 3 (Sport); the selected mode is never changed optimistically.

ACE slots show the printer's `consumables_percent`, including a reported zero.
Missing values display **Unknown**; fill is never fabricated from material or
color. Empty slots have no fill bar. Whether RFID supplies a useful estimate
depends on the tag and firmware. An October 28 live read reported zero for the
one occupied slot; a nearly empty RFID spool comparison is pending. Click an
ACE humidity or temperature reading for the existing five-minute history.
Bambu K-factor values and commands are not applied to Anycubic filament.

### ACE hover menus and spool assignment

Hover or click a filament slot for the shared filament/inventory card. **Assign
Spool** opens the same inventory picker as a Bambu slot. The native assignment
links that physical box/slot to inventory without changing printer settings,
replaying a Bambu preset, or promising to configure it when a spool is inserted.
**Configure** separately sends the native material type and RGB color while the
printer is idle. RFID-locked material settings cannot be overwritten. Exact
variants such as PLA Matte are retained rather than replaced with generic PLA.

RFID-identified slots automatically link to the built-in inventory only when
the exact color HEX and full material name match one active physical spool.
Inventory material plus subtype form the full name: **PLA / Matte** matches
**PLA Matte**, while **PLA**, **PLA Basic** and **PLA+** remain distinct. Case,
whitespace and the optional `#` are formatting; six-digit RGB equals opaque
eight-digit RGBA ending `FF`. There is no approximate color matching.

Two matching active spools, two fresh RFID slots with the same metadata, a
multicolor spool, or a spool already assigned elsewhere prevent automatic
assignment. Archived spools do not count. Automatic links are rechecked when
fresh inventory arrives and removed if the match becomes ambiguous or the
reported spool changes. Existing manual links are preserved. Unknown, stale
or offline reports do not remove links. This feature creates no spools,
changes no RFID data, sends no printer commands and is inactive in Spoolman
mode. The LAN report currently provides no unique RFID UID, so identical
physical spools require manual assignment.

Load/unload use the selected physical ACE and slot and require current idle
telemetry. Stop also remains available during that connection's own manual
feed operation, without interrupting a print. **Refresh** reads the latest
material status. A force-RFID-read
command has not been established for this LAN protocol, so **Re-read RFID** is
shown disabled with that explanation. Hover an ACE name to view its reported
serial/firmware and save a friendly name; absent metadata displays as unknown.

Inventory links require `inventory:update`, friendly names `printers:update`,
and physical commands `printers:control`; existing printer scope still applies.

The print dialog shares Bambu's collapsible Filament Mapping and Print Options
sections, swatch/HEX slot picker, descriptions and Off/On controls. Only native
options reported by the selected printer are shown; native boolean settings
do not offer Bambu's Auto mode.

### ACE drying

Open **Drying** under either ACE unit. Set temperature, hours and minutes, then
start or apply the cycle. Each unit has its own controls and reported target,
remaining minutes and on/off state. Stop sends only the native off operation.
Commands report **queued** until subsequent native status arrives; a setting
receipt does not fabricate an active cycle or restart a local countdown.

The native request uses `multiColorBox/setDry`, the physical box ID and minutes.
Durations are 5–1440 minutes. The reported accessory model identifies the base
ceiling: [ACE Pro](https://store.anycubic.com/products/anycubic-ace-pro) is limited
to 55°C and [ACE 2 Pro](https://store.anycubic.com/products/anycubic-ace-2-pro) to
65°C. The recovered slicer rules further limit the whole unit for loaded
materials, including PLA at 55°C and certain variants at eight hours when using
65°C. Unknown accessory IDs and stale drying metadata cannot enable a heater.
Drying heats every spool in that ACE; review the material and spool heat limits.

Drying remains independent of printing and can be requested during a print.
This does not enable queue-based auto-drying or Bambu drying automation. A
read-only check confirmed both connected units as model 40002 with drying off;
no physical drying cycle was started or stopped by the implementation tests.
The operator subsequently confirmed that starting a drying cycle works on
the Synology-connected S1. Changing or stopping a cycle still needs separate
hardware acceptance.

### Light, jog, home and extruder controls

The chamber-light button uses the reported type-2 light. Native XYZ jog sends
an explicit axis, direction and bounded distance up to 50 mm, with 1/10/50 mm
buttons. Home options preserve the requested **XYZ**, **XY** or **Z** operation.
Motion, material edits and manual filament feed are disabled while printing.
These paths never send Bambu G-code or change endstop settings.

E buttons are short native extrusion/retraction pulses, with a backend stop
scheduled after 0.5 seconds and an explicit **Stop E** action. They require
reported extrusion capability, an idle printer and nozzle temperature of at
least 170°C; E movement is not presented as a measured millimetre distance.
Stale timer callbacks cannot send to a replacement connection, and a fresh
stopped report is required before another pulse. Heating, motion and feed
acceptance on hardware remains pending; tests send none of these commands to
the physical printer.

## Upload without starting a print

The native browser uses the shared File Manager header, toolbar, breadcrumb,
file icons and footer. It lists actual printer folders and retains native
pagination. Filtering and name/size sorting apply to the current page, as
indicated in the browser. Bambu-only shortcuts and unimplemented download,
delete and preview actions are not offered.

Upload confirmation searches the full validated catalog snapshot on firmware
that ignores its filename-search parameter. Local pagination is applied after
that search, so uploads beyond the first page can still be confirmed. Both
the unique filename and exact byte size must match before queued printing.

Open **Browse printer files**, choose `.gcode` sliced with Anycubic Slicer Next
or OrcaSlicer for the saved printer model, and select **Upload**. Validation
requires exact model metadata and text G-code. Projects, renamed binary files,
missing/mismatched model headers and unexpanded templates are rejected. The
maximum size is 512 MiB. Matching metadata is a compatibility check, not
certification of sliced settings; review the export in your slicer.

The card's **Upload** button and raw `.gcode` drag/drop open this native uploader.
The shared File Library also accepts validated Anycubic G-code and retains its
model metadata, instead of applying the Bambu-only raw-G-code rejection to it.
Raw exports cannot enter the Bambu print queue or FTPS dispatcher. Uploading a
library file does not start a print or automatically transfer it to a printer.

The backend obtains a fresh upload endpoint from the current pinned LAN MQTT
session, uploads once to that printer's HTTP port 18910 and verifies the unique
filename and byte size in the native catalog. Progress, errors and catalog
pages use this native transport. Both printer-file and printer-control
permission are required. Uploads do not queue or start a print.

New names have unique suffixes to avoid overwriting existing files. A cancelled
or interrupted transfer can leave a complete or partial file on the printer.
Inspect its catalog before retrying; transfers are never replayed automatically.
In-process progress expires after ten minutes and is lost on server restart.
Files are not automatically deleted from the printer.

On October 28, an upload-only hardware test on S1 firmware 2.7.2.7 transferred
an existing 8,530,707-byte S1-sliced export and confirmed
`bambuddy-lan-upload-test-97572bad47bc.gcode` with the matching size in the live
catalog. The probe rejected all non-read MQTT operations, including print
start. The file remains on the printer. Other models need separate upload
acceptance.

## Queued LAN printing

Use **Print** on a sliced `.gcode` file in the File Library. Choose the matching
Anycubic printer. This first version targets one specific printer per queue
request; quantities, staged/manual start, ASAP and scheduled jobs use the
existing queue. Model load balancing and cross-model variants remain Bambu
features. The file must identify its slicer, exact printer model, material and
color for every used zero-based tool. The server re-reads the actual file at
admission and dispatch rather than trusting editable library metadata.

The shared printer selector remains visible when only one printer exists or a
printer was preselected from its card. Its name, model, address, selection and
reported status remain visible before submission.

Selecting several native G-code files offers **Queue files**. This queues every
selected file in the arranged order on one matching printer; it does not create
one job with alternative slices. All sources must validate for the same model.
The filament panel combines their distinct full material/color requirements,
then writes each file's original tool numbers and its own mapping to its queue
item. Quantity applies to each file. A partial submission stops at the first
error and keeps only the remaining files available for retry in that dialog.

Print settings show the selected printer's reported leveling, vibration,
flow calibration and timelapse capabilities. With ACE enabled, explicitly map
each used tool to a physical box and slot. Loaded material labels and sliced
material variants must agree; a missing, removed or stale mapped spool blocks
dispatch. With ACE disabled, only a single used tool is accepted. Wire indexes
are resolved from the current sorted native box inventory, including negative
IDs where supported; Bambu tray IDs and K profiles are never substituted.

Dispatch creates a raw G-code history copy, uploads under a unique name and
confirms the catalog size before sending the native LAN print-start request.
The queue matches the exact unique upload filename to observe actual printing
and completion. A returned job ID provides an additional identity check when
the firmware supplies one; signed LAN job IDs are supported. Stock S1 firmware
can report `task_id: -1` for a running LAN print. In that case, the dispatch's
unique upload filename identifies the job, while the original library filename
alone cannot confirm it. Conflicting assigned IDs are never ignored. A matched
active report clears the start warning, including after Bambuddy restarts;
completion still requires a subsequent matched terminal report. The next
queued job waits for **Plate cleared**. That gate survives a Bambuddy restart.

Unconfirmed native queue items do not borrow a printer's live progress or ETA.
Those fields become available after the queue matches the running job.

An ambiguous start, lost matching job or restart during submission keeps a
durable printer hold. It is never automatically replayed. Check the printer
and use **Confirm printer is not printing** only after verifying it is idle;
the unresolved outcome is recorded as failed, without a hardware command.
Clear and acknowledge the build plate before the next queued job can start.
Stop is allowed only for the matching active queued job and waits for the
printer's terminal report. Deleting source files cannot erase a submitted
job's no-replay record. Another batch copy needs the original library file.

Native print settings are stored separately from Bambu options. Bambu heat
soak, G-code injection, nozzle/rack mappings, cost-center billing, automatic
power-off and outcome confirmation are not offered for native jobs. Native
filament consumption is not guessed from Bambu callbacks. Existing queue
review, ownership and printer-scope permissions still apply.

The queue uses the same native connection state as the printer card. An online
S1 does not require a smart plug. Connected, idle printers awaiting native
readiness data show **Waiting for Anycubic print readiness**; this does not
bypass fresh-status, active-job or plate-clear checks. Offline printers retain
the existing offline/Auto On behavior.

Protocol, dispatch, cancellation, restart, permission and UI tests use isolated
fixtures. No physical print was started to validate this implementation; a
small supervised S1 print, ACE mapping and scheduled second job still need
operator acceptance before unattended use.

A separate read-only capability check on the physical S1 reported all four
native print options and both ACE units. The printer was already printing,
and dispatch readiness correctly stayed false. That probe blocked every
non-read MQTT operation; it did not start or stop a print.

## LAN transport

The provider uses HTTP port **18910** for the native LAN bootstrap and MQTT over
TLS on **9883**. Discovered endpoints are bound to the entered printer IP and
approved ports. HTTP redirects and environment proxies are not used. A changed
TLS certificate requires a new discovery and explicit review in printer settings.
The initial identity/fingerprint inspection assumes the selected local network
is trusted; use a known device fingerprint when independently available.

ACE unit IDs and slot indices remain physical addresses. A unit is displayed
as four slots, and multiple units remain separate. S1 Max inventories can contain
four ACE units. The X's built-in feeder keeps its physical ID `-1`, separate
from external ACE units; it is not labeled as a dry box or given humidity and
drying-temperature readings. An empty, unknown or disconnected
slot must not inherit old filament information. Material length is not displayed
as a percentage. This display adapter does not enable Bambu AMS mutations.

## API and compatibility

Existing records migrate to `printer_type="bambu"`. Bambu connection arguments
and operations retain their existing behavior. New Anycubic records use
`printer_type="anycubic"`, a supported canonical `model`, and a reviewed, 64-character hex
`tls_fingerprint`. Their Bambu `access_code` is empty and automatic FTPS archive
is disabled. Changing a saved printer to a different protocol is rejected.
The pairing response must match the discovered model ID, and the connection
must match the saved model. MQTT subscriptions, report filtering and command
topics use that model ID; another model's topic is never accepted as telemetry.

Each supported model has its own bundled transparent printer icon. The S1 icon
comes from the user-supplied photograph. See [icon sources and generation prompts](anycubic-icons.md)
for the other model references. Icons load locally, without vendor image requests.

`POST /api/v1/printers/anycubic/discover?ip_address=192.168.1.50` requires the
existing printer-create permission and returns only public discovery fields.
Control routes retain the existing printer-control permission. Unsupported
printer operations return a deliberate 4xx response before protocol work.
Editing a saved printer uses
`POST /api/v1/printers/{id}/anycubic/discover?ip_address=192.168.1.50` with the
printer-update permission and its existing printer scope. The discovered
identity must match the saved device.

Anycubic status updates bypass Bambu calibration, spool-preset writes and archive
automation. Native queue jobs use their own identity-matched observer. Bambu virtual-printer targets, built-in Bambu camera
requests, live K-profiles and FTPS file operations are blocked. External camera
configuration remains available.

## Built-in LAN camera

Open the printer card's camera button in a window or overlay. Bambuddy starts
LAN capture through the existing pinned MQTT session, reads HTTP-FLV on the
printer's port `18088` and uses its bundled ffmpeg to deliver MJPEG through
Bambuddy's HTTPS endpoint. No Anycubic account, cloud relay or external-camera
URL is required. Capture may illuminate the chamber; it sends no print or
motion controls. Bambu light, object-skipping and diagnostic controls are hidden.

Viewers and snapshots share one upstream. Closing the last viewer releases the
HTTP connection and decoder after the normal grace period; it does not send a
global `stopCapture` that would interrupt another slicer's camera view. Kobra
4/X use the reported session path after their camera initialization sequence.
Device-reported hosts cannot redirect capture away from the configured private
printer address; redirects and environment proxies are disabled. Session paths
remain on the backend and are omitted from logs and ffmpeg arguments.

On October 28, 2026, a camera-only LAN probe received and decoded a live JPEG
from the physical Kobra S1 on firmware 2.7.2.7. The operator subsequently reported
the Synology camera working; the other models still need acceptance. External
cameras remain available as an override.

Each browser viewer keeps its ID across reconnects. A replacement connection
ends the previous response for that same viewer and account; Stop closes only
its matching response while other viewers retain the shared upstream. Token
renewal closes the old live image request before opening its replacement.
This prevents renewed camera responses from accumulating in the browser's
HTTP connection pool. Fixture tests cover Anycubic, Bambu and external cameras;
the reported browser freeze recovered after closing and reopening its tab.
Acceptance of the fix on the Synology remains pending after deployment.

## Protocol provenance and the prior Orca plugin

The user's previous independent Anycubic Orca plugin (`0.2.0.dev5`) supplied
native-code-backed protocol research for the LAN bootstrap, topic envelopes,
physical ACE inventory, print and catalog schemas. Its 73 top-level Python
modules matched the recorded dev5 source manifest during this review. A fresh
focused offline run passed 143 tests; the historical 717-test run was not rerun.

Useful references are `lan.py`, `mqtt.py`, `materials.py`, `lan_trust.py` and
the native serializer evidence. The existing Orca native-filament adapter is
deliberately unregistered because Orca's generic fallback can substitute PLA
for PLA Matte/PLA+ or select an unrelated preset. That host-specific adapter is
not used by Bambuddy.

The new provider is an independent implementation. No installed Anycubic DLL,
private key, frontend bundle, patched Orca host or AGPL OrcaCubic code is copied.
The MIT project [anycubic_ha_local](https://github.com/chrisfore/anycubic_ha_local)
and its [protocol notes](https://github.com/chrisfore/anycubic_ha_local/blob/main/research/PROTOCOL-VALIDATED.md)
provide corroborating public protocol evidence. Original and newer `/info`
formats need separate fixtures. Native serializer agreement and offline tests
do not establish physical command, camera, transfer or catalog acceptance.

## Upstream contribution preparation

A public fork audit checked 457 direct and 12 indirect fork repositories.
README/branch-name inspection succeeded for 467 of those 469 repositories;
two were unavailable. Nine multi-brand default-branch trees and targeted code
searches in six multi-brand forks revealed no Anycubic implementation. This is
evidence that no public implementation was found, not proof that no private or
unadvertised work exists.

Upstream [CONTRIBUTING.md](https://github.com/maziggy/bambuddy/blob/dev/CONTRIBUTING.md)
requires maintainer agreement and issue assignment before a PR, a `dev` base,
companion wiki documentation and screenshots for UI changes. Multi-brand
acceptance is not established; [issue 2935](https://github.com/maziggy/bambuddy/issues/2935)
discusses a separate custom-printer proposal. This implementation and its
documentation still need those upstream contribution requirements addressed
before opening a PR.

Suggested issue scope: an experimental Kobra S1 LAN provider with explicit
capabilities, read-only two-ACE inventory, reviewed TLS pairing and three basic
controls and native settings, native G-code queue dispatch, file browsing and
transfer, and optional Cloud Profiles/MakerOnline access preserving Bambu defaults.
Keep native queue lifecycle and settings distinct from Bambu archival and AMS
automation. Cloud profile writes remain outside the current scope. The
[Cloud Profiles guide](anycubic-cloud.md) covers its token-only connection,
permissions and current storage-host limitation.

## Physical acceptance checklist

- Pair the actual S1; verify its identity and certificate and observe both ACE
  units and eight correctly addressed slots.
- Check empty/unknown slots, slot changes, unplug/reconnect, printer restart and
  certificate mismatch. Old session callbacks must not restore old material.
- Observe a print started from the supported slicer, including pause/resume and
  completion. Compare temperatures, layer/progress and time units to the printer.
- With the operator present, exercise pause/resume/stop and verify the hardware
  response. A queued MQTT message alone is not acceptance.
- Confirm unavailable actions stay unavailable and a Bambu printer alongside
  the S1 retains its existing operation.

Public identity/certificate discovery and a 22-second authenticated read-only
LAN session passed on a Kobra S1 running firmware 2.7.2.7. The provider received
15 usable reports, including preparation/preheating state, temperatures,
layers and remaining time. This initial check had one ACE connected. After the
second ACE was connected, a separate inventory read during printing confirmed
both raw and normalized unit IDs 0 and 1, each with physical slot indices 0–3.
The first unit had two occupied and two empty slots; the second had four empty
slots. Stale material fields on empty slots were cleared correctly. Both
sessions sent only read queries, disconnected afterward, and did not persist
account, printer or trust settings.

Restart/reconnect behavior, material changes and physical pause/resume/stop
still need acceptance. The optional Cloud Profiles
integration has not been tested with a live account.

The original reviewed S1 firmware allowed certificate-only discovery. A
firmware variant that requires a client certificate during a TLS 1.2 discovery
handshake needs an additional bootstrap-aware discovery flow and is currently
outside this provider's acceptance evidence.

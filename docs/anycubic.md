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
| ACE inventory | Model-specific physical unit IDs and four slots per unit; read-only |
| Print controls | Pause, resume and stop; MQTT delivery is not printer acknowledgement |
| Built-in LAN camera | Camera-only activation, HTTP-FLV decoding, shared browser MJPEG and snapshots |
| External camera | Existing configured external camera support |
| File transfer, archive, queue dispatch | Unavailable in this provider |
| ACE drying, load/unload, slot edits, calibration, firmware | Unavailable in this provider |
| Anycubic Cloud Profiles | Optional read-only account integration; see [Cloud Profiles setup](anycubic-cloud.md) |

LAN monitoring and control do not require an Anycubic account. The optional
Cloud Profiles tab uses a separate token connection to browse and export
slicer presets; it never sends cloud printer commands.

## Adding a printer

1. Enable LAN mode on the printer and give Bambuddy access to its local network.
2. Add a printer and select **Anycubic**. Enter its literal private IP address.
3. Discover the printer, review the displayed identity and SHA-256 certificate
   fingerprint, then explicitly trust that certificate before saving.
4. Use a descriptive name. Discovery selects the actual printer model and its
   matching icon. Review that model before saving. `Anycubic` prefixes, `Combo`
   suffixes and the `KS1`/`KS1 Max` aliases normalize to canonical names.

Discovery returns the firmware's `cn` identity where available, or its reported
`usn` on original firmware. It does not invent a Bambu serial or access code.
The saved certificate digest is public and can be read through the printer API.
Broker passwords, bootstrap tokens and client private keys are session-private.

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
automation. Queue dispatch, Bambu virtual-printer targets, built-in Bambu camera
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
from the physical Kobra S1 on firmware 2.7.2.7. The updated Synology browser path
and the other models' cameras still require operator acceptance. External
cameras remain available as an override.

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
controls, plus an optional, separately scoped Cloud Profiles browser preserving
Bambu defaults. Treat file upload/printing, archives, Cloud
profile writes and ACE mutations as separately reviewed additions. The
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

# Anycubic Kobra S1 LAN support (experimental)

This provider supports an original Kobra S1 in stock firmware LAN mode, with
one or two four-slot ACE units. It is a first integration of monitoring and
basic controls. A read-only physical check passed on one S1 with one ACE;
two-unit operation and physical print controls still need acceptance.

| Feature | Initial implementation |
| --- | --- |
| LAN pairing and connection | Signed bootstrap and explicitly reviewed TLS certificate |
| Printer status | State, temperatures, progress, layers and remaining time |
| ACE inventory | Up to two units and eight distinct physical slots; read-only |
| Print controls | Pause, resume and stop; MQTT delivery is not printer acknowledgement |
| External camera | Existing configured external camera support |
| Built-in camera, file transfer, archive, queue dispatch | Unavailable in this provider |
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
4. Use a descriptive name. The model remains **Kobra S1**.

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
as four slots, and two units remain separate. An empty, unknown or disconnected
slot must not inherit old filament information. Material length is not displayed
as a percentage. This display adapter does not enable Bambu AMS mutations.

## API and compatibility

Existing records migrate to `printer_type="bambu"`. Bambu connection arguments
and operations retain their existing behavior. New Anycubic records use
`printer_type="anycubic"`, `model="Kobra S1"`, and a reviewed, 64-character hex
`tls_fingerprint`. Their Bambu `access_code` is empty and automatic FTPS archive
is disabled. Changing a saved printer to a different protocol is rejected.

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
documentation remain local until publication is requested.

Suggested issue scope: an experimental Kobra S1 LAN provider with explicit
capabilities, read-only two-ACE inventory, reviewed TLS pairing and three basic
controls, plus an optional, separately scoped Cloud Profiles browser preserving
Bambu defaults. Treat file upload/printing, built-in camera, archives, Cloud
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
layers and remaining time. One ACE unit reported four slots: two occupied and
two empty; the empty slots cleared correctly. No second unit was observed.
The session sent no printer control commands and did not persist the account,
printer or trust settings.

Both ACE units together, restart/reconnect behavior and physical
pause/resume/stop still need acceptance. The optional Cloud Profiles
integration has not been tested with a live account.

The original reviewed S1 firmware allowed certificate-only discovery. A
firmware variant that requires a client certificate during a TLS 1.2 discovery
handshake needs an additional bootstrap-aware discovery flow and is currently
outside this provider's acceptance evidence.

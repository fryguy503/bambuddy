This experimental provider implements signed stock Anycubic Kobra LAN monitoring and
pause/resume/stop. It does not establish physical printer acceptance. Camera,
file transfer, print submission, drying, filament writes and Bambu-specific
features remain unsupported.

Protocol evidence comes from the user's Anycubic Orca plugin 0.2.0.dev5 and
its independently audited Anycubic Slicer Next 2.0.0.3 native serializers.
The original hardware evidence was a Kobra S1 on firmware 2.7.2.7 with two
ACE 2 Pro units on firmware 1.1.36. A temporary read-only check of this provider
on an S1 running 2.7.2.7 received live print-preparation telemetry and one
four-slot ACE unit. A subsequent read after connecting the second unit confirmed
both physical IDs 0 and 1, each with slots 0–3, in raw and normalized inventory
while printing. Empty-slot metadata was cleared correctly. Both sessions sent
only status queries, then disconnected; no controls, material changes or
restart/reconnect behavior were tested.
The private certificate-file utility adapts the user-owned plugin utility.
Other implementation code was written for this repository.

Public corroboration, consulted for protocol facts rather than copied code:

- https://github.com/chrisfore/anycubic_ha_local/blob/main/research/PROTOCOL-VALIDATED.md
- https://github.com/chrisfore/anycubic_ha_local/blob/main/LICENSE
- https://github.com/chrisfore/anycubic_ha_local/blob/main/custom_components/anycubic/const.py

The allowlist covers Kobra 3 (20024), S1 (20025), 3 Max (20026), 3 V2 (20027),
4 (20028), S1 Max (20029) and X (20030). Pairing binds the decrypted model ID
to discovery and the saved model, and every MQTT topic uses the paired model.
Offline tests exercise each model's bootstrap, topic routing and report
isolation. Additional-model support does not constitute hardware acceptance.
S1 Max can project four ACE units; X preserves its negative-ID built-in feeder
alongside external units and suppresses nonexistent dry-box measurements.

No Anycubic executable, private key, extracted frontend resource, AGPL
OrcaCubic implementation or unlicensed MQTT bridge code is redistributed.
Dependencies use Bambuddy's existing paho-mqtt and cryptography packages.

Older S1 `/info` replies expose `usn` and `modelName` without `cn` or
`modelId`. Discovery uses the validated USN as the saved device identity
when a physical serial is unavailable. Pairing also accepts a previously
known MQTT deviceId, but always checks the selected model, address and pin.
Pairing keys, passwords, certificate material and camera/upload URLs never
enter public status, diagnostic logs or persisted provider state.

An approved SHA-256 certificate fingerprint is mandatory. A certificate-only
discovery probe supplies a candidate fingerprint; discovery does not grant
trust. Reconnects use that same explicit pin and re-bootstrap credentials.
TLS server verification and certificate validity checks occur before any
application traffic. Mutual TLS material, when supplied, is loaded from
current-user-only temporary files and removed immediately afterward.

ACE reports preserve physical box IDs 0/1 and slot indices 0–3. Full getInfo
snapshots require all four slots per reported unit, and replace inventory;
other actions merge only into established full units. Unknown RFID data stays
unknown. Status 4 clears stale material even when firmware repeats its type.
The normalized `ams` objects serve Bambuddy's display only; they never authorize
Bambu commands, automatic spool assignment or Bambu archival callbacks.

MQTT queue success is not device acceptance. Controls require current printer
lifecycle telemetry, use a new message ID and are never replayed on reconnect.
Monotonic clocks bound telemetry and control freshness independently of system
clock changes. Temperature/progress-only reports do not renew lifecycle authority;
pausing or stopping does not authorize resume. Retained reports and recent duplicate
message IDs cannot renew a new session's state. Remaining print time is reported
in minutes, matching Bambuddy's normalized display state.
Offline tests use synthetic payloads and loopback TLS peers exclusively.

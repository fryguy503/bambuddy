# Experimental Anycubic LAN support, Cloud Profiles and MakerOnline

Bambuddy's Bambu MQTT, FTPS, camera and AMS automation cannot communicate with
an Anycubic Kobra S1. This change adds an independent LAN provider for status,
temperatures, progress and read-only inventory for two ACE units/eight slots,
plus pause/resume/stop, bounded temperature/fan/speed controls, native light and
idle jog/home controls, G-code upload and native queue dispatch,
and shared LAN camera viewing/snapshots. ACE displays reported remaining
percentages and opens the existing sensor history. Shared ACE/filament hover
cards offer friendly names and inventory assignment, with separate native
material/color, load/unload and per-box drying controls. A separate, optional Anycubic Cloud Profiles tab reads
the account's native slicer-configuration ZIP for browsing and JSON export.

Native RFID slots can link to the built-in inventory by exact RGBA and full
material/subtype only when both the active spool and reported slot are unique.
Ambiguous or assigned-elsewhere candidates stay unassigned; manual links are
preserved. These links never configure the printer or invent a tag UID.

LAN onboarding requires explicit review of the printer's TLS fingerprint.
The Add Printer dialog can scan a private IPv4 subnet for LAN-mode Anycubic
printers, then populate the selected device for certificate review. Scanning
does not pair or change printer state; Docker uses the same unicast path.
Existing printers remain Bambu by default. Unsupported Bambu operations are
blocked before transport or automation work; Anycubic status cannot trigger
Bambu archives, calibration, preset writes, FTPS uploads or virtual-printer
bridging.

Native queued printing accepts an exact-model raw G-code library source and
physical ACE mapping for a specific printer. The print dialog shows only
reported native start options; ordinary Bambu sources retain the existing
workflow. The scheduler validates/uploads the source, persists submission
intent before publishing, and correlates actual progress and terminal reports
by unique upload filename and, when available, native task ID. Stock S1 LAN
reports with a `-1` task sentinel can confirm the exact submitted filename;
conflicting assigned task IDs remain blocked. Ambiguous starts hold the printer
across restart until observed or explicitly resolved; they are never replayed.
A raw G-code history snapshot is stored independently of Bambu archival.
Native sliced time and weight populate the existing queue estimates, including
a bounded refresh of older queued files. A matched successful completion charges
explicit per-tool grams to unchanged built-in inventory assignments captured
before submission, using the shared atomic usage writer and usage history.
The charge commits with the native completion journal so duplicate reports and
restart recovery cannot deduct twice. Partial prints, changed assignments,
Spoolman reporting and older jobs without a start snapshot are not guessed.
Native billing, automatic power-off, outcome confirmation,
model load balancing and cross-model alternatives are not enabled. Queue tests
use isolated fixtures. The operator has reported a queued print running; a
read-only S1 check observed its unique upload filename, running progress and
`task_id: -1`. Recovery of that job with this fix and terminal completion still
need deployment acceptance. No physical print-start command was sent by these
checks.

The Cloud tab accepts an existing Anycubic user token or Slicer Next access
token in the fixed Global or China region. Global slicer tokens are exchanged
for a validated workbench user token. Account credentials are encrypted, owner-scoped and subject to
the existing API-key Cloud-access fence and new read/auth permissions. Profiles
retain native settings and material variants; connection secrets are masked in
details and export. Unknown types remain visible. Nothing is automatically
imported into the shared Local Profiles store or Bambu filament-ID resolver.

MakerOnline is registered with the existing model-provider interface. Its Model
Sources tab resolves pasted links and imports model/project files into an owned
library folder. It reuses the encrypted Global Slicer SSO token from Profiles,
requires Cloud read plus library upload permission, and checks the account
revision before persisting a download. Workbench tokens are not sent to the
website; account headers are not sent to signed storage URLs.

Cloud access sends no printer commands and does not change LAN operation.
Cloud profile writes, password/OAuth login and Cloud print submission are not
included. Forced RFID reread and Bambu K-factor operations remain unavailable.
The upload-only LAN endpoints obtain the current session endpoint,
validate the sliced model, use a unique filename and confirm its catalog size;
they never start or enqueue a print. Uncertain transfers are not replayed.
Configuration downloads currently admit only the observed Anycubic storage
host; another regional host needs separate verification. This is experimental
support, not full feature parity or completed hardware/account acceptance.

## Validation

- Backend coverage includes LAN protocol/onboarding, TLS checks, two-ACE state,
  reconnect and stale-callback behavior, provider capability fences and existing
  Bambu regressions. Cloud tests use mocked HTTP, synthetic configuration ZIPs
  and disposable databases for token validation, encryption, account isolation,
  API-key permissions and bounded archive parsing.
- Frontend checks cover token-only connection, account changes and expiry,
  permissions, unknown profile types, native JSON export and existing
  Bambu/Orca profile behavior. TypeScript, affected ESLint, all 15 locales,
  production build and the Safari 16 browser baseline have passed locally.
- Public identity/certificate discovery and authenticated read-only telemetry
  passed on a physical Kobra S1 with firmware 2.7.2.7. A short session received
  usable state, temperature, layer and remaining-time data. After the second ACE
  was connected, a separate read confirmed both physical unit IDs and all eight
  distinct slots in raw and normalized inventory, with correct occupied/empty
  slots. No control command was sent. Material changes, restart/reconnect and
  physical pause/resume/stop acceptance remain pending. No live Cloud account
  acceptance is claimed.
- An upload-only physical S1 test transferred an existing 8,530,707-byte sliced
  export and confirmed its unique filename and matching size in the catalog.
  The probe blocked every non-read MQTT operation, including print start.
  File Library uploads also admit validated native G-code with model metadata;
  the Bambu queue and dispatcher still reject that format. The printer card's
  direct uploader/drop path never enters the Bambu library/print flow.
  The operator confirmed that starting an ACE drying cycle worked.
  Physical acceptance of light, motion, material/feed controls,
  heater/fan/speed changes and RFID remaining estimates
  is pending. No settings were changed by these tests.
- A camera-only LAN probe activated the S1 camera and decoded a live JPEG.
  Multiple-viewer, snapshot and cleanup checks use isolated fixtures; the
  operator has since reported the Synology camera working. Other models remain
  unverified. MakerOnline has public API/fixture validation; a real account
  download test remains pending.
- The exact Linux CI environment and full upstream CI pipeline were not run.

## Before submission

- Obtain maintainer agreement and an assigned issue, as required by
  CONTRIBUTING.md. Native multi-brand support and the optional Cloud profile
  scope still need upstream agreement.
- Target upstream `dev`. This branch is based on
  `cab3c8e5d92de38ae4b4de05d9915a0eab41cd5b`.
- Prepare companion wiki documentation from `docs/anycubic.md` and
  `docs/anycubic-cloud.md`, link its PR, and prepare any required website update.
  Capture the required before/after UI screenshots.
- Record LAN hardware and Cloud account acceptance separately using the setup
  guides. Verify any additional account download host before admitting it.

Source is published on the fork's testing branch. An upstream issue/PR, wiki
contribution and release have not been submitted by this change.

# Experimental Kobra S1 LAN support and optional Anycubic Cloud Profiles

Bambuddy's Bambu MQTT, FTPS, camera and AMS automation cannot communicate with
an Anycubic Kobra S1. This change adds an independent LAN provider for status,
temperatures, progress and read-only inventory for two ACE units/eight slots,
plus pause/resume/stop. A separate, optional Anycubic Cloud Profiles tab reads
the account's native slicer-configuration ZIP for browsing and JSON export.

LAN onboarding requires explicit review of the printer's TLS fingerprint.
Existing printers remain Bambu by default. Unsupported Bambu operations are
blocked before transport or automation work; Anycubic status cannot trigger
Bambu archives, calibration, preset writes, queued uploads or virtual-printer
bridging.

The Cloud tab accepts an existing Anycubic user token in the fixed Global or
China region. Account credentials are encrypted, owner-scoped and subject to
the existing API-key Cloud-access fence and new read/auth permissions. Profiles
retain native settings and material variants; connection secrets are masked in
details and export. Unknown types remain visible. Nothing is automatically
imported into the shared Local Profiles store or Bambu filament-ID resolver.

Cloud access sends no printer commands and does not change LAN operation.
Cloud profile writes, password/OAuth login, built-in camera playback, printer
file transfer/print submission, archival and ACE mutations are not included.
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

No source, issue, PR, documentation or release was published during this work.

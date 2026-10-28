# Experimental Kobra S1 LAN provider

Bambuddy's Bambu MQTT, FTPS, camera and AMS automation cannot communicate with
an Anycubic Kobra S1. This change adds an independent, LAN-only provider for
printer status, temperatures, print progress and read-only inventory for up to
two ACE units/eight slots, plus pause/resume/stop.

New onboarding reviews the printer's TLS fingerprint explicitly. Existing
printers remain Bambu by default. Unsupported Bambu operations are blocked
before transport or automation work; a new Anycubic status cannot trigger
Bambu archives, calibration, preset writes, queued uploads or virtual-printer
bridging. No Anycubic cloud login or online service is used.

Built-in camera playback, file transfer/print submission, archival, cloud
access, ACE drying/load/unload and material writes are not included. This is
experimental support, not full feature parity or hardware acceptance.

## Validation

- 386 focused backend tests and 15 subtests passed in the final run, including
  the Anycubic protocol, onboarding, controls, printer manager, Bambu bridge,
  K-profile and backup regressions. Other runs separately cover the broader
  API/printer-scope and scheduler/camera suites; their counts overlap.
- 38 dedicated provider tests include loopback TLS verification, legacy
  firmware discovery, two-ACE state, lifecycle freshness, reconnects and stale
  callback rejection. These are included in the final backend run above.
- Ruff lint and formatting passed across all backend files. Basedpyright
  1.40.2 passed in baseline lock mode across 414 application files, using the
  local Windows Python 3.12 environment. Linux's exact CI environment was not run.
- Frontend TypeScript, affected ESLint, all 15 locales, production build and
  Safari 16 browser baseline passed. A broader affected run passed 232 tests;
  the final UI review fixes passed 86 relevant tests. These are overlapping runs.
- No physical printer operations were performed. Hardware validation is
  deferred because the printer is printing and is not currently in LAN mode.

## Before submission

- Obtain maintainer agreement and an assigned upstream issue, as required by
  CONTRIBUTING.md. Native support for additional manufacturers is not yet an
  agreed upstream scope.
- Target upstream `dev`. This branch is based on
  `cab3c8e5d92de38ae4b4de05d9915a0eab41cd5b`.
- Prepare the required companion wiki change from `docs/anycubic.md` and link
  its PR. Capture the required before/after UI screenshots.
- Record hardware acceptance separately using the checklist in the setup guide.

No source, issue, PR, documentation or release was published during this work.

# Printer plugin connection diagnostics

SDK **2.3.0** supports plugin-owned diagnostics in Add Printer, Edit Printer,
the saved-printer diagnostic modal and support diagnostics. A plugin supplies
the tests and their text; the application supplies authorization and the
existing checklist, status icons, review and retry controls.

## Implement the hook

Declare `requires_api = ">=2.3.0,<3.0.0"` and implement:

```python
from backend.app.printers.sdk import (
    ConnectionDiagnosticResult, DiagnosticCheck, network_diagnostic_checks,
)

async def connection_diagnostics(
    self, config, *, connection=None, wait_for_publish_seconds=0.0, progress=None,
):
    checks = await network_diagnostic_checks(config.ip_address)
    # Run bounded, provider-specific probes using this exact configuration.
    fresh = connection is not None and await self.has_fresh_status_report(connection)
    checks.append(DiagnosticCheck(
        id="example:telemetry",
        status="skip" if connection is None else "pass" if fresh else "warn",
        title="Live printer status",
        message="No saved connection is available." if connection is None
                else "The provider verified a fresh status report." if fresh
                else "The current status report is stale or incomplete.",
        hint="Complete pairing, then retry." if connection is None
             else None if fresh else "Check the status subscription, then retry.",
    ))
    statuses = {check.status for check in checks}
    return ConnectionDiagnosticResult(
        printer_id=config.printer_id,
        ip_address=config.ip_address,
        overall="problems" if "fail" in statuses else
                "warnings" if "warn" in statuses else "ok",
        checks=checks,
    )
```

`has_fresh_status_report` above is a provider-implemented observation helper,
not a host SDK method. It must verify actual freshness and valid telemetry.

`config` is the standard `PrinterConfig`: provider ID, optional saved printer
ID, address, identity, access code, model and plugin-specific options. For a
pre-save inspection the host does not require successful connection validation
first: the hook can explain missing pairing information or an unapproved
certificate. Do not approve a newly observed certificate automatically.

For a saved printer, `connection` is its existing provider connection when the
stored configuration matches. Observe it without disconnecting, re-pairing or
replacing it. Never use diagnostics to start prints, upload test files, move
axes, change heating, or silently activate a camera. An optional end-to-end
camera test belongs in a separate, explicit operation.

When no connection exists, a provider may open a temporary authenticated
observation session after checking the approved identity/certificate. Keep its
callbacks separate from host print accounting. Close its sockets, stop its
worker and await cleanup on success, failure and cancellation. The host retains
the provider runtime through the awaited hook and cancellation cleanup.

Each network probe needs its own deadline. The host's new-hook budget is
45 seconds; cancellation cleanup must also be bounded. Blocking sockets and
OS inspection must run off the event loop. `wait_for_publish_seconds` is an
observation allowance, not permission to reconnect a working printer.
`progress`, if supplied, can expose `checks` and the current `stage` to a
support-bundle caller.

## Result fields

`ConnectionDiagnosticResult` contains `printer_id`, `ip_address`, `overall`
(`ok`, `warnings`, `problems`) and up to 128 checks. The result must identify
the requested printer. Non-network providers may use an empty address.

Each `DiagnosticCheck` contains:

| Field | Meaning |
| --- | --- |
| `id` | Unique stable provider-owned ID, up to 128 characters; namespace new IDs such as `example:control-port` |
| `status` | `pass`, `fail`, `warn`, or `skip` |
| `title` | Optional plain-text heading, up to 200 characters |
| `message` | Optional plain-text observation, up to 2,000 characters |
| `hint` | Optional plain-text recovery action, up to 2,000 characters |
| `params` | Legacy translation interpolation values; optional, defaults to `{}` |

The UI displays supplied text as escaped text. It does not execute HTML,
Markdown, links or scripts. When presentation fields are omitted, existing
host translations keyed by check ID/status remain available. Unrecognized IDs
have a readable fallback; adding checks does not require a new frontend build.

Report a critical connection failure as `fail`; an optional service problem or
network advisory as `warn`; an untestable or inapplicable feature as `skip`.
Set the overall severity from the results. A TCP-open result proves only port
reachability. Separate certificate matching, authenticated subscription and
fresh valid telemetry into their own checks. Do not mark unsupported hardware
as failed or report stale status as a successful live observation.

`network_diagnostic_checks(address)` provides bounded host-environment,
container-mode, route-source and subnet observations. It does not send printer
commands or scan the subnet. Bridged networking and routed subnets are
advisories because unicast printer protocols can work in either configuration.
Unsupported addresses or unavailable platform facts produce skipped checks.

Use fixed public error messages. Do not copy raw exceptions, pairing tokens,
passwords, private keys, authenticated URLs or certificate material into results,
logs or recovery hints.

## HTTP and offline schemas

`POST /api/v1/printers/diagnostic` requires printer-create permission and accepts
`plugin_id`, `ip_address`, optional `serial_number`, `access_code`, `model` and
`plugin_options`. `GET /api/v1/printers/{id}/diagnostic` uses saved configuration
and enforces printer-read permission and printer scope. Existing providers with
only `diagnostic_operation` keep the legacy argument list.

Export machine-readable schemas without contacting hardware:

```console
python -m backend.app.printers.sdk schema diagnostic
python -m backend.app.printers.sdk schema diagnostic-check
```

Installation validates the hook signature without executing probes. Test
unreachable services, rejected credentials, changed certificates, stale status,
optional hardware, timeouts, cancellation cleanup and a working saved connection
that must remain untouched.

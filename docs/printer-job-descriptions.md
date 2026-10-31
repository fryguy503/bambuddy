# File-specific print setup (SDK 2.2)

`PrinterPlugin.describe_job(config, artifact, path)` inspects an authorized local
artifact and returns a `JobDescription`. It must not send printer commands. The
base implementation derives option fields and defaults from `job.start`; a
provider can override it to parse its native sliced files.

The host exposes this hook at
`POST /api/v1/printers/{printer_id}/job-description` with exactly one of
`library_file_id` or `archive_id`, and optional `plate_id`. The caller needs read
access to both the target printer and the source file. Paths are resolved by the
host; plugins receive an existing local file and the authorized artifact URI.
The response contains no local file path.

The description returns the artifact, input requirements, JSON Schema for
`options`, defaults, warnings and optional presentation metadata. Each input has
its original sliced input ID, material and color, optional gram estimate, and
compatible tool/source resource IDs. An input's `routing_key` is an explicit
provider assertion that equivalent material requirements may share a selection
across files. Omit it when equivalence is unknown. The host otherwise qualifies
input IDs by artifact URI and never assumes that two files' `T0` inputs match.

Batch setup combines equivalent inputs in one shared form, then maps each choice
back to every file's original IDs and validates all files before queue creation.
Queue jobs remain separate. `job.start` validates the final typed request and
native file again; the setup response does not authorize a printer action.

Optional `extensions["bambuddy:ui"].material_mode` may contain `option` (a boolean
option schema key), `external_source_id`, `enabled_help`, `disabled_help`,
`source_placeholder` and `require_reported_material`. When the last flag is true,
the form offers only available sources with a known reported matching material.
Other options render from their schema titles, descriptions and defaults.
Use `requires_api: ">=2.2.0,<3.0.0"` when a package imports this contract.

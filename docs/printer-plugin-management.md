# Printer plugin installation, updates and recovery

Printer plugins are versioned Python packages. The application includes the
initial `bambu` package, and an administrator can install a newer compatible
Bambu package without replacing Bambuddy or publishing an application release.
A server restart applies a selected version; updates do not hot-swap code while
printer connections or requests are running.

The [authoring reference](printer-plugin-authoring.md) describes device resources,
operation schemas and the SDK. This guide describes package delivery and the
operational lifecycle.

## Package format

A `.bbp` file is a ZIP archive with this layout:

```text
manifest.json
signature.json                 # optional publisher signature
payload/
  __init__.py
  plugin.py
  transport.py
  assets/...
```

`manifest.json` is a UTF-8 JSON `PluginManifest`. Its `files` object names every
file relative to `payload/`, with lowercase SHA-256 digests. Neither the root
manifest nor the optional signature is part of that payload inventory. The
entrypoint `plugin:ExamplePrinterPlugin` names a factory within the payload.
Nested modules such as `vendor.plugin:ExamplePrinterPlugin` are also supported.

Own implementation imports must be relative, for example `from .transport
import Client`. Import the shared SDK through `backend.app.printers.sdk` and
`backend.app.printers.contracts`. An update must not import the application's
bundled `backend.app.printers.bambu` implementation or its historical service
aliases: that would pin the new package to old protocol code.

Versions contain `major.minor.patch`, with optional supported prerelease/local
version components. An installed version is immutable. Publish a new version
when changing its archive; retries with identical bytes are idempotent. A
package claiming the current bundled Bambu version must match that bundled
manifest and inventory, rather than replacing that version with changed code.

Build the bundled Bambu package from its plugin source:

```console
python scripts/build_printer_plugin.py --output dist/bambu-1.0.0.bbp
```

The builder packages plugin code and resources, writes its manifest and hashes,
and excludes application code, databases and compiled Python caches. For a new
Bambu plugin release, update the plugin's own version and model/feature metadata
before building. The package's `requires_core`, `requires_api`, `requires_python`
and dependency ranges must accurately describe the versions it supports.

## Installation and activation

Use Settings → Printer plugins while logged in as an administrator.

Printer plugins has its own entry in the Settings menu. You can also open
`/settings?tab=printer-plugins` directly, or search Settings for “plugins”.

1. Upload the package. This stages files and validates their static metadata;
   it does not import the uploaded code or replace the running plugin.
2. Review the plugin, version, publisher ID, integrity result and signature
   result. SHA-256 integrity alone does not authenticate a publisher.
3. Select **Activate after restart** for the staged version. The server checks
   affected printers and validates all saved `plugin_options` against the new
   configuration schema before changing the desired version.
4. Restart the Bambuddy server during an idle period. The running version stays
   visible until this restart; the application does not restart itself.
5. Check the loaded version and any startup errors in the management screen.

Updates are refused while an affected printer is printing or paused, a queue
dispatch/upload is in flight, firmware is uploading, or an SDK operation receipt
is accepted/running. A plugin may also refuse deactivation through
`can_deactivate(config, connection)` for hardware-specific operations such as
calibration or a tool change.

First activation can restore a missing provider even when its saved queue or
operation records remain active. This recovery applies only when the provider
is not loaded and none of its printers has a managed connection. Live print,
upload and dispatch checks still apply. Activation does not clear queue records
or replay commands; after restart the restored provider can observe the printers
and let the host reconcile their state.

Package management requires an actual administrator when authentication is
enabled. A user granted `settings:update` without administrator status cannot
install executable code, and an administrator-owned API key cannot do so either.
When authentication is disabled, the application retains its existing single
trust-domain behavior.

## What validation establishes

Before any uploaded Python is imported, the server checks:

- Manifest schema, identity/version syntax, declared API/core/Python ranges,
  operation/configuration JSON schemas and known permission identifiers.
- Required dependency distributions and their installed versions. The server
  does not run `pip`, download dependencies or invoke package setup hooks.
- The complete payload inventory and SHA-256 hashes, optional expected archive
  SHA-256, ZIP integrity, Python syntax and package-relative implementation
  imports.
- Portable relative paths, case collisions, duplicate ZIP entries, traversal,
  symbolic/special files, encrypted entries and bounded expanded/upload sizes.
- Publisher identity continuity for an existing plugin, and a verified trusted
  Ed25519 signature when the package is signed. Once an installed plugin has a
  signed version, a later update cannot silently switch to an unsigned archive.
- Existing saved printer options when selecting or rolling back a version.

At startup, selected archives and extracted files are verified again before
import. The loader uses an isolated module namespace for each package version,
then checks that the resulting `PrinterPlugin` matches the static identity,
version and API and satisfies the SDK contract. Malformed lifecycle signatures
or invalid declared schemas and device templates prevent that candidate from
being selected.

Runtime device descriptions, snapshots, operation arguments and results are
validated at the API boundary as described in the authoring reference.

These checks establish format, integrity and API compatibility. They do not
certify physical printer behavior, make arbitrary third-party Python safe, or
isolate it from the application process. Install code from a source the
administrator trusts. Hardware/firmware acceptance still requires the author's
test matrix and live-device testing.

## Signatures and publisher keys

Signatures are optional for a first administrator-approved installation. Unsigned
packages are explicitly labeled `administrator-installed-code`; they are not
presented as cryptographically authenticated.

An administrator can register an Ed25519 public key with
`POST /api/v1/printer-plugins/trust-keys`:

```json
{
  "key_id": "example-vendor-2026",
  "publisher_id": "example-vendor",
  "public_key": "BASE64_ENCODED_32_BYTE_ED25519_PUBLIC_KEY"
}
```

The package's optional `signature.json` contains:

```json
{
  "algorithm": "ed25519",
  "key_id": "example-vendor-2026",
  "signature": "BASE64_ENCODED_SIGNATURE"
}
```

Sign canonical UTF-8 JSON for the complete root manifest: keys sorted, separators
`,` and `:`, and Unicode preserved (`ensure_ascii=False`). This is the format
returned by `backend.app.printers.packages.canonical_manifest()`. It covers the
manifest's entire file inventory and metadata. Keep private signing keys with
the publisher; only the public key is uploaded to Bambuddy.

The trusted key must belong to the manifest's `publisher_id`. Keys cannot be
silently reassigned to another publisher or public key. For rotation, add a new
key ID for the same publisher, release a newly signed version, and retain older
trusted keys while rollback packages still require them. Revoking a key causes
packages using that key to fail subsequent activation/startup verification.

The bundled Bambu publisher ID is `bambuddy`. That ID alone is an ownership label,
not proof that an unsigned download came from the project. Verify its source or
use a publisher key obtained through a trusted channel.

## State, storage and recovery

The default store is `DATA_DIR/printer-plugins` (or the application data directory
when `DATA_DIR` is unset). `PRINTER_PLUGIN_DIR` can override the store location.
Persist this directory across Docker recreation and include it in operational
backups along with the database:

```text
printer-plugins/
  state.json
  packages/<plugin-id>/v-<version-key>/
    package.zip
    manifest.json
    payload/...
```

Version directories are staged and renamed atomically. A process-safe lock
serializes management writes; the selection file is replaced atomically. The
running plugin is not unloaded by a request.

The management API distinguishes the loaded version, desired version, previous
selection, last successfully loaded version, enabled state and restart-needed
state. A selected package that fails verification/import/contract validation
falls back only to an earlier version of the same plugin. Bambu can also fall
back to its bundled version. The failed candidate and fallback remain visible
as a load error and desired/loaded-version mismatch.

There is no fallback from a missing or disabled non-Bambu plugin to Bambu.
Configured printer identities remain unchanged and the unavailable provider
fails explicitly.

Select **Rollback** to choose the previous version, or explicitly choose
`bundled` for Bambu. Rollback also requires an idle provider and validates saved
options against the target schema. Package activation/rollback never rewrites
printer options or deletes fields. Authors must keep schemas backward
compatible or document an explicit user-controlled configuration transition;
schema defaults do not silently migrate stored data.

Disabling or removing a plugin with configured printers is refused. Remove or
reassign those printer records first. The included Bambu package can be disabled
when unused, but the application cannot delete its built-in fallback. Unselected
installed versions may be deleted; selected, loaded and rollback versions are
protected. Removing a non-bundled plugin schedules package removal at restart,
after its old Python modules are no longer running.

Python entry-point packages installed by the host package manager remain visible
as `entry-point` / host-managed providers. They are not silently taken over by
an uploaded archive. Explicitly remove the host-managed distribution before
migrating that plugin ID to managed package installation.

## Administrative HTTP operations

All routes below require administrator authority; existing read-only catalog
routes continue to use normal `printers:read` permission.

| Method and path under `/api/v1/printer-plugins` | Effect |
| --- | --- |
| `GET /management` | Loaded/desired versions, installed versions, errors and policy |
| `POST /packages` | Multipart `file`, optional `expected_sha256`; validate and stage |
| `POST /{id}/activate` | JSON `{"version":"1.2.0"}`; select for restart |
| `POST /{id}/rollback` | Optional JSON `{"version":"1.1.0"}`; select prior/explicit version |
| `POST /{id}/enable` | Re-enable an installed selection for restart |
| `POST /{id}/disable` | Disable an unused provider for restart |
| `DELETE /{id}/versions/{version}` | Remove an unselected, unprotected archive |
| `DELETE /{id}` | Schedule an unused non-bundled provider's removal |
| `POST /trust-keys` | Register a publisher's public Ed25519 key |
| `DELETE /trust-keys/{key_id}` | Revoke that trusted public key |

Uploads are limited to 64 MiB, 128 MiB expanded, 16 MiB per file and 2,048 archive
entries. Manifest JSON is limited to 512 KiB. Native extension binaries are not
installed by the package loader; declare required preinstalled distributions in
the manifest instead. Validation errors return an explicit failure without
importing the candidate. Selecting a version returns `restart_required`; it is
not a claim that the running provider was already replaced.

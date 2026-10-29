# Anycubic Cloud Profiles (experimental)

The optional **Profiles → Anycubic Cloud** tab reads the slicer presets already
synced to an Anycubic account. It can browse, search, filter, inspect and export
them. Printer monitoring and control continue through the separate
[Kobra S1 LAN provider](anycubic.md); connecting this account does not change a
printer's network mode or enable cloud printer commands.

## Connect an account

1. Open **Profiles → Anycubic Cloud**.
2. Choose **Global** or **China**, matching the account. Their API values are
   `global` and `china`.
3. Paste an existing **Slicer Next access token** or an Anycubic Cloud website
   **user token** (`XX-Token`). The Orca plugin's Slicer Next access token uses
   the same exchange. Follow **How to get an access token** for the
   [web-token instructions](https://github.com/Nino6689/hass-anycubic#option-b--web-token)
   if needed. Paste only the value, without a header name or `Bearer` prefix.
4. Select **Set Token**. Bambuddy verifies the account identity before saving
   the connection and loading its presets.

For Global accounts, a Slicer Next JWT with `tokenType=access-token` is first
exchanged at `POST /v3/public/loginWithAccessToken`. That request has the access
token in its JSON body and no `XX-Token` header. The returned workbench user
token is validated with `userInfo` and saved encrypted. The original Global
Slicer SSO token is retained in the same encrypted connection for optional
MakerOnline downloads. The tokens have different authorities: the workbench
token is never substituted for MakerOnline SSO. Website user tokens and China-region tokens use the
existing direct validation path. Unverified JWT claims only select the exchange;
the vendor's exchange and account response establish authentication.

This integration supports token sign-in only. It does not offer email/password
login, embedded desktop OAuth or automatic credential extraction. An OAuth ID
token is not a substitute for the Cloud user token. If the token expires or is
revoked, obtain a new one and reconnect. **Disconnect** clears Bambuddy's saved
connection and profile caches; it does not revoke the token at Anycubic.

## MakerOnline model sources

Open **Model Sources → MakerOnline**, paste a model-page link, resolve its files,
and import a 3MF, STL or STEP into your own library. Files land in a MakerOnline
folder with provider provenance and scoped duplicate detection. Multiple plates
of the same complete 3MF appear once. Slice imported projects for the intended
printer before printing; importing never uploads to a printer or starts a job.

Downloads reuse the Global Slicer Next SSO saved above. Reconnect an older
connection with a current Slicer Next access token to retain it. A workbench
website user token alone can still read Profiles but does not enable MakerOnline
downloads. China-region credentials are not sent to the Global website.

The provider uses the website's fixed `www.makeronline.com/api/mold/detail` and
`/api/file/get-download-url` contracts. It sends raw SSO Authorization only to
that website. Signed file URLs are restricted to observed vendor storage hosts
and fetched with a separate client without account headers or cookies.
Authentication, gated content and website challenges are reported as errors;
the integration does not bypass them. The original site remains available for
browser login, browsing, licensing and any verification required by a model.

Viewing requires `anycubic_cloud:read`; importing also requires `library:upload`.
An API key needs its owner and Allow Cloud Access. A logout/account revision
check fences a download before saving. Signing out clears both encrypted
credentials. Public metadata and fixture downloads were validated; actual
MakerOnline account-token acceptance still requires an operator login test.

If the account has no presets, sync the desired configuration from Anycubic
Slicer Next and select **Refresh**. The printer can remain in LAN mode.

## Browse and export

Profiles are grouped as **Filament**, **Process**, **Printer**, and **Other
profile types**. Search matches names; the type filter narrows those groups.
Unknown profile types remain available for inspection rather than being
silently discarded. **Refresh** requests a fresh account archive instead of
using the normal five-minute cache.

Open a profile to view its native JSON, then choose **Export JSON** to download
that profile. Native settings, material variants, values and inheritance
references are preserved. Connection credentials inside preset settings are
masked in both the displayed and exported JSON. An individual JSON export is
not a full configuration ZIP or a resolved set of parent presets; review it in
the matching slicer before importing or using it.

This tab does not create, edit, delete or upload Cloud profiles. It does not
automatically import them into the shared Local Profiles library, expose them
as Bambuddy slicing presets, convert their IDs to Bambu filament IDs, or apply
them to ACE slots or Bambu K-profiles. ACE drying, material edits and cloud
printer commands remain outside this integration.

## Account storage and permissions

With Bambuddy authentication enabled, each user has an independent Anycubic
connection. When authentication is disabled, the installation shares one
connection. Tokens are encrypted with Bambuddy's existing encryption key;
saving a token requires working encryption and never falls back to plaintext.
Keep the encryption key available when restoring the database.
If an old connection cannot be decrypted, **Disconnect** can clear it without
the old key. Restore working encryption before saving another token.

The **Cloud** permission category contains:

| Permission | Allows |
| --- | --- |
| `anycubic_cloud:auth` | Connect or disconnect the caller's Anycubic account |
| `anycubic_cloud:read` | Browse, inspect and export that account's profiles |

Connection status accepts either permission. An API key also needs an owner
and the existing **Allow Cloud Access** scope; it uses its owner's connection.
Profile identities and caches are scoped to the account. Logout, token changes
and rejected sessions invalidate cached account data.

## Network and current limitations

Account API traffic uses verified HTTPS to fixed regional origins:

| Region | Origin |
| --- | --- |
| Global | `https://cloud-universe.anycubic.com` |
| China | `https://cloud-platform.anycubicloud.com` |

The provider requests the native slicer configuration with
`POST /p/p/workbench/api/v3/user/getSlicerConfig`, then reads its ZIP archive.
It accepts downloads only from the observed HTTPS storage host
`workbentch.s3.us-east-2.amazonaws.com` (the vendor's spelling). Other hosts,
redirects and unverified URLs are rejected. Accounts or regions returning a
different storage host may therefore fail to load profiles until that host is
verified and supported. This restriction has not been validated against a live
China-region account. Account credentials are never sent to the storage host.

Archives are limited to 16 MiB downloaded, 64 MiB expanded, 2,000 entries and
2 MiB per JSON file. Unsafe archive paths, duplicate entries and malformed
profile JSON are rejected. A failed download or invalid archive is reported
as an error, not an empty account. Built-in material catalogs and Cloud writes
are not exposed by this initial tab.

## API

| Route | Purpose |
| --- | --- |
| `GET /api/v1/anycubic-cloud/status` | Local connection state and supported sign-in methods |
| `POST /api/v1/anycubic-cloud/token` | Validate and save `{token, region}` |
| `POST /api/v1/anycubic-cloud/logout` | Clear the connection |
| `GET /api/v1/anycubic-cloud/profiles` | Grouped profile metadata; `?refresh=true` bypasses the cache |
| `GET /api/v1/anycubic-cloud/profiles/{id}` | Native profile JSON with connection secrets masked |

The implementation has local mocked-HTTP, archive-parser, account-isolation,
permission and UI validation. It has not been accepted against a real Anycubic
account. LAN discovery and physical printer acceptance are separate checks.

"""MakerOnline model imports using the optional Profiles account's SSO token.

The public website's model-detail and open-in-slicer download contracts are
used directly. No browser-cookie extraction or cloud printer control occurs.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx

from backend.app.core.auth import is_auth_enabled
from backend.app.core.permissions import Permission
from backend.app.services.anycubic_cloud_credentials import AnycubicCredentials, ensure_current, load_credentials
from backend.app.services.model_providers.base import (
    ModelProvider,
    ProviderAuthConfig,
    ProviderAuthError,
    ProviderAuthType,
    ProviderDownload,
    ProviderDownloadInfo,
    ProviderForbiddenError,
    ProviderNotFoundError,
    ProviderResolvedModel,
    ProviderResourceRef,
    ProviderService,
    ProviderStatus,
    ProviderUnavailableError,
    ProviderUrlError,
)

ORIGIN = "https://www.makeronline.com"
STORAGE_HOSTS = (
    "acop-prod.s3.us-east-2.amazonaws.com",
    "cloud-slice-prod.s3.us-east-2.amazonaws.com",
    "cdn-acop.makeronline.com",
)
EXTENSIONS = (".3mf", ".stl", ".step", ".stp")


def _id(value) -> str:
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,15}", text):
        raise ProviderUrlError("Invalid MakerOnline model or file identifier.")
    return text


def _filename(value) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 240
        or not value.lower().endswith(EXTENSIONS)
        or any(c in value for c in '/\\:%"<>|?*')
        or any(ord(c) < 32 for c in value)
    ):
        raise ProviderUnavailableError("MakerOnline returned an unsupported model filename.")
    return value


def _storage_url(value: str) -> str:
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https"
            and url.hostname in STORAGE_HOSTS
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and not url.fragment
            and value.isascii()
            and len(value) <= 16384
            and not any(ord(c) < 33 for c in value)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderUnavailableError("MakerOnline returned an unsupported storage host.")
    return value


class MakerOnlineProvider(ModelProvider):
    source_type = "makeronline"
    display_name = "MakerOnline"
    host_patterns = ("makeronline.com",)
    default_folder_name = "MakerOnline"
    view_permission = Permission.ANYCUBIC_CLOUD_READ
    import_permission = Permission.ANYCUBIC_CLOUD_READ
    additional_import_permissions = (Permission.LIBRARY_UPLOAD,)
    auth = ProviderAuthConfig(
        auth_type=ProviderAuthType.ACCESS_TOKEN,
        display_label="Anycubic Cloud sign-in",
        description="Reuses the encrypted Slicer Next SSO token connected on Profiles.",
        setup_hint="In Profiles, connect Anycubic Cloud with a Global Slicer Next access token.",
    )

    async def build_service(self, *, db, user, api_key_owner=None, client=None):
        owner = user if user is not None else api_key_owner
        credentials = (
            AnycubicCredentials() if owner is None and await is_auth_enabled(db) else await load_credentials(db, owner)
        )
        return MakerOnlineService(credentials, db=db, owner=owner, client=client)

    def parse_url(self, url: str) -> ProviderResourceRef:
        match = None
        if not isinstance(url, str) or len(url) > 2048:
            raise ProviderUrlError("Paste a MakerOnline model page URL.")
        candidate = url.strip()
        if "://" not in candidate:
            candidate = "https://" + candidate
        try:
            parsed = urlsplit(candidate)
            match = re.fullmatch(
                r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?model/(?:[^/]+/)?([1-9][0-9]{0,15})\.html/?", parsed.path
            )
            valid = (
                parsed.scheme == "https"
                and parsed.hostname in ("makeronline.com", "www.makeronline.com")
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and match
            )
        except ValueError:
            valid = False
        if not valid or match is None:
            raise ProviderUrlError("Paste a MakerOnline model page URL.")
        return ProviderResourceRef(self.source_type, match[1], original_url=candidate)

    def canonical_url(self, ref):
        base = f"{ORIGIN}/en/model/{_id(ref.external_id)}.html"
        return base + (f"#file-{_id(ref.sub_id)}" if ref.sub_id else "")

    def source_url_filter(self, column, external_id):
        base = self.canonical_url(ProviderResourceRef(self.source_type, external_id))
        return (column == base) | column.like(base + "#file-%")

    def download_hosts(self):
        return STORAGE_HOSTS


class MakerOnlineService(ProviderService):
    def __init__(self, credentials: AnycubicCredentials, *, db=None, owner=None, client=None):
        self._credentials, self._db, self._owner = credentials, db, owner
        self._token = (
            credentials.makeronline_token if credentials.region == "global" and not credentials.invalid else None
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)

    async def close(self):
        self._token = None
        if self._owns_client:
            await self._client.aclose()

    async def validate_before_save(self):
        if self._db is not None:
            await ensure_current(self._db, self._owner, self._credentials)

    async def get_status(self, db):
        return ProviderStatus(authenticated=bool(self._token), can_download=bool(self._token))

    async def _bytes(self, response, maximum):
        if response.status_code in (401, 403):
            raise ProviderAuthError("MakerOnline requires a current SSO token. Reconnect Anycubic Cloud in Profiles.")
        if response.status_code == 404:
            raise ProviderNotFoundError("MakerOnline model or file was not found.")
        if response.status_code != 200:
            raise ProviderUnavailableError("MakerOnline is unavailable or requires a website verification step.")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > maximum:
                raise ProviderUnavailableError("MakerOnline response exceeds the download limit.")
            content.extend(chunk)
        return bytes(content)

    async def _api(self, method, path, **kwargs):
        headers = {"Accept": "application/json", "lang": "en"}
        if self._token:
            # The website sends the raw mo_access_token, without 'Bearer'.
            headers["Authorization"] = self._token
        try:
            async with asyncio.timeout(30):
                async with self._client.stream(method, ORIGIN + path, headers=headers, **kwargs) as response:
                    raw = await self._bytes(response, 4 * 1024 * 1024)
            body = json.loads(raw)
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            raise ProviderUnavailableError("MakerOnline returned an unavailable or invalid response.") from None
        if not isinstance(body, dict) or body.get("code") != 0 or "data" not in body:
            raise ProviderForbiddenError(
                "MakerOnline refused this request. Check the account and model on its website."
            )
        return body["data"]

    async def _model(self, ref):
        model_id = _id(ref.external_id)
        data = await self._api("GET", "/api/mold/detail", params={"id": model_id})
        if not isinstance(data, dict) or _id(data.get("id")) != model_id:
            raise ProviderUnavailableError("MakerOnline returned a different model.")
        return data

    def _files(self, data):
        groups: list[tuple[dict | None, object]] = [(None, data.get("files", []))]
        profiles = data.get("print_profiles", [])
        if not isinstance(profiles, list) or len(profiles) > 500:
            raise ProviderUnavailableError("MakerOnline returned invalid profiles.")
        groups.extend((profile, profile.get("files", [])) for profile in profiles if isinstance(profile, dict))
        files = {}
        for profile, records in groups:
            if not isinstance(records, list) or len(records) > 1000:
                raise ProviderUnavailableError("MakerOnline returned invalid model files.")
            for record in records:
                if not isinstance(record, dict):
                    continue
                name = record.get("file_name") or record.get("filename") or record.get("name")
                if not isinstance(name, str) or not name.lower().endswith(EXTENSIONS):
                    continue
                name = _filename(name)
                native_id = _id(record.get("file_id") or (profile or {}).get("id") or record.get("id"))
                paths = record.get("simple_url", [])
                if (
                    not isinstance(paths, list)
                    or len(paths) > 8
                    or any(not isinstance(p, str) or len(p) > 4096 for p in paths)
                ):
                    raise ProviderUnavailableError("MakerOnline returned invalid file paths.")
                file = {
                    "id": int(native_id),
                    "filename": name,
                    "paths": paths,
                    "key": record.get("key"),
                    "printer": str((profile or {}).get("printer_label", ""))[:512],
                }
                if native_id in files and any(files[native_id][key] != file[key] for key in ("filename", "key")):
                    raise ProviderUnavailableError("MakerOnline returned ambiguous file identifiers.")
                # Multiple plates often point to the same complete 3MF.
                files[native_id] = file
        return files

    async def resolve(self, ref):
        data = await self._model(ref)
        files = self._files(data)
        await self.validate_before_save()
        return ProviderResolvedModel(
            ref,
            {
                "title": str(data.get("title", "MakerOnline"))[:512],
                "url": ref.original_url or makeronline_provider.canonical_url(ref),
                "license": data.get("license"),
            },
            [{"id": file["id"], "filename": file["filename"], "printer": file["printer"]} for file in files.values()],
        )

    async def get_download(self, ref):
        if not self._token:
            raise ProviderAuthError("Connect Anycubic Cloud in Profiles with a Global Slicer Next access token first.")
        files = self._files(await self._model(ref))
        selected = files.get(_id(ref.sub_id)) if ref.sub_id else next(iter(files.values()), None)
        if selected is None:
            raise ProviderNotFoundError("This MakerOnline model file is no longer available.")
        paths = selected["paths"]
        index = 1 if selected["filename"].lower().endswith(".3mf") else 0
        if len(paths) <= index:
            raise ProviderForbiddenError(
                "MakerOnline did not grant a download. Open the model on its website to check access."
            )
        urls = await self._api("POST", "/api/file/get-download-url", json={"path": paths})
        url = urls.get(paths[index]) if isinstance(urls, dict) else None
        if not isinstance(url, str):
            raise ProviderUnavailableError("MakerOnline did not return a download URL.")
        return ProviderDownloadInfo(
            ProviderResourceRef("makeronline", ref.external_id, str(selected["id"])),
            _storage_url(url),
            selected["filename"],
        )

    async def download(self, info):
        url = _storage_url(info.url)
        try:
            async with asyncio.timeout(180):
                # A separate client guarantees account Authorization/cookies
                # never accompany presigned storage requests.
                async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as storage:
                    async with storage.stream("GET", url) as response:
                        data = await self._bytes(response, 256 * 1024 * 1024)
        except (httpx.HTTPError, TimeoutError):
            raise ProviderUnavailableError("MakerOnline file download failed. Try again later.") from None
        if not data or (info.suggested_filename.lower().endswith(".3mf") and not data.startswith(b"PK\x03\x04")):
            raise ProviderUnavailableError("MakerOnline returned an empty or invalid model file.")
        return ProviderDownload(data, _filename(info.suggested_filename))

    async def fetch_thumbnail(self, url):
        raise ProviderNotFoundError("MakerOnline previews are opened on its website.")


makeronline_provider = MakerOnlineProvider()

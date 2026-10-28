"""Optional, read-only Anycubic Cloud profile access over verified HTTPS.

This service accepts a Cloud user token (the web ``XX-Token``) or exchanges
a user-supplied Slicer Next access token. It never discovers credentials,
opens cloud MQTT, or sends printer commands. The native public-header signer
was recovered independently in the user's Anycubic Slicer Next audit; its
application constants are protocol identifiers, not account credentials.

Account contract: https://github.com/Nino6689/anycubic-cloud-client/blob/main/docs/PROTOCOL.md
Token help: https://github.com/Nino6689/hass-anycubic#option-b--web-token
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json as json_module
import time
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

Region = Literal["global", "china"]
API_PREFIX = "/p/p/workbench/api"
CLOUD_ORIGINS = MappingProxyType(
    {"global": "https://cloud-universe.anycubic.com", "china": "https://cloud-platform.anycubicloud.com"}
)
USER_INFO_PATH = "/user/profile/userInfo"
TOKEN_EXCHANGE_PATH = "/v3/public/loginWithAccessToken"
SLICER_CONFIG_PATH = "/v3/user/getSlicerConfig"
TOKEN_HELP_URL = "https://github.com/Nino6689/hass-anycubic#option-b--web-token"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONFIG_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_TOKEN_LENGTH = 16_384
CONFIG_DOWNLOAD_HOST = "workbentch.s3.us-east-2.amazonaws.com"  # Vendor spelling, confirmed in the installed client.
_REQUEST_DEADLINE_SECONDS = 20.0
_DOWNLOAD_DEADLINE_SECONDS = 30.0

# Observed native read endpoints only. In particular, sendOrder, setSlicerConfig,
# printer properties, uploads, and cloud MQTT credentials are not admitted.
_ALLOWED_ENDPOINTS = frozenset(
    {
        ("GET", USER_INFO_PATH),
        ("POST", TOKEN_EXCHANGE_PATH),
        ("POST", SLICER_CONFIG_PATH),
        ("POST", "/v5/machine_material/getMaterialListByBrand"),
    }
)
_PUBLIC_PREFIX = "f9b3528877c94d5c9c5af32245db46ef"
_PUBLIC_MIDDLE = "0cf75926606049a3937f56b0373b99fb"
_HEADER_VERSION = "V3.0.0"
_USER_AGENT = "Bambuddy/1.0 (+https://github.com/maziggy/bambuddy)"


class AnycubicCloudError(Exception):
    """A fixed, safe diagnostic that excludes credentials and response bodies."""

    def __init__(self, message: str = "Anycubic Cloud request failed.", *, code: str = "cloud_error") -> None:
        super().__init__(message)
        self.code = code


class AnycubicCloudAuthError(AnycubicCloudError):
    def __init__(self) -> None:
        super().__init__("Anycubic Cloud did not accept this user token.", code="invalid_auth")


class AnycubicCloudUnavailableError(AnycubicCloudError):
    def __init__(self) -> None:
        super().__init__("Anycubic Cloud is temporarily unavailable. Try again later.", code="cloud_unavailable")


def _token_text(token: object) -> str:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_TOKEN_LENGTH
        or not token.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
    ):
        raise AnycubicCloudError("Enter a valid Anycubic Cloud user token.", code="invalid_token_format")
    return token


def _is_slicer_access_token(token: str) -> bool:
    """Choose the fixed exchange endpoint; the vendor verifies authentication.

    Unverified JWT claims are used only as a format hint, never an identity.
    Opaque web tokens and China-region user tokens retain their existing path.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json_module.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        return False
    return isinstance(claims, dict) and claims.get("tokenType") == "access-token"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("nonfinite JSON")


def _headers(token: str, region: Region) -> dict[str, str]:
    stamp = str(time.time_ns() // 1_000_000)
    nonce = str(uuid.uuid4())
    message = _PUBLIC_PREFIX + stamp + _HEADER_VERSION + _PUBLIC_MIDDLE + nonce + _PUBLIC_PREFIX
    signature = hashlib.md5(message.encode("ascii"), usedforsecurity=False).hexdigest().upper()
    return {
        "XX-Nonce": nonce,
        "XX-Signature": signature,
        "XX-Timestamp": stamp,
        "XX-Device-Type": "pcf",
        "XX-Version": _HEADER_VERSION,
        "XX-LANGUAGE": "US",
        "XX-IS-CN": "1" if region == "china" else "0",
        "XX-Token": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }


async def _response_bytes(response: httpx.Response, maximum: int) -> bytes:
    length = response.headers.get("content-length")
    if length and (not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > maximum):
        raise AnycubicCloudError("Anycubic Cloud response is too large.", code="response_too_large")
    payload = bytearray()
    async for chunk in response.aiter_bytes():
        if len(payload) + len(chunk) > maximum:
            raise AnycubicCloudError("Anycubic Cloud response is too large.", code="response_too_large")
        payload.extend(chunk)
    return bytes(payload)


class AnycubicCloudService:
    """A per-account session; storage and caller authorization belong to routes.

    An injected client is useful for tests and remains owned by its caller.
    Redirects are refused on each request, even if that client's default differs.
    Neither construction nor closing opens a network connection.
    """

    def __init__(
        self,
        token: str | None = None,
        region: Region = "global",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(region, str) or region not in CLOUD_ORIGINS:
            raise AnycubicCloudError("Select a valid Anycubic Cloud region.", code="invalid_region")
        self._token = None if token is None else _token_text(token)
        self._region: Region = region
        self._client = client
        self._owns_client = client is None
        self._closed = False
        self._account_id: str | None = None
        self._config_download_url: str | None = None

    def __repr__(self) -> str:
        return f"AnycubicCloudService(region={self._region!r}, credentials=<redacted>)"

    @property
    def region(self) -> Region:
        return self._region

    @property
    def account_namespace(self) -> str:
        if self._account_id is None:
            raise AnycubicCloudAuthError()
        return f"anycubic:{self._region}:{self._account_id}"

    @property
    def account_token(self) -> str:
        """Sensitive workbench user token, for encrypted credential storage only."""
        if self._token is None or self._account_id is None:
            raise AnycubicCloudAuthError()
        return self._token

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._closed = True
        self._token = None
        self._account_id = None
        self._config_download_url = None

    async def __aenter__(self) -> AnycubicCloudService:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect_token(self, token: str) -> dict[str, str]:
        """Validate before replacing a previously accepted account token."""
        candidate = _token_text(token)
        if self._region == "global" and _is_slicer_access_token(candidate):
            exchange = await self._request(
                "POST",
                TOKEN_EXCHANGE_PATH,
                token=candidate,
                json={"device_type": "pcf", "access_token": candidate},
            )
            data = exchange.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
                raise AnycubicCloudAuthError()
            candidate = _token_text(data["token"])
        body = await self._request("GET", USER_INFO_PATH, token=candidate)
        identity = self._identity(body, candidate)
        self._token = candidate
        self._account_id = identity["user_id"]
        self._config_download_url = None
        return identity

    async def validate_token(self) -> dict[str, str]:
        self._account_id = None
        self._config_download_url = None
        if self._token is None:
            raise AnycubicCloudAuthError()
        return await self.connect_token(self._token)

    @staticmethod
    def _identity(body: dict[str, Any], token: str | None) -> dict[str, str]:
        data = body.get("data")
        if data is None or (isinstance(data, dict) and data.get("id") is None):
            raise AnycubicCloudAuthError()
        if not isinstance(data, dict):
            raise AnycubicCloudError("Anycubic Cloud returned an invalid account response.", code="invalid_response")
        raw_id = data.get("id")
        user_id = str(raw_id)
        if (
            isinstance(raw_id, bool)
            or not user_id.isascii()
            or not user_id.isdecimal()
            or len(user_id) > 32
            or int(user_id) < 1
        ):
            raise AnycubicCloudError("Anycubic Cloud returned an invalid account response.", code="invalid_response")

        def safe_text(value: object, maximum: int) -> str | None:
            if (
                not isinstance(value, str)
                or len(value) > maximum
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                return None
            if token and token in value:
                return None
            return value.strip() or None

        display_name = safe_text(data.get("user_nickname"), 200) or user_id
        result = {"user_id": user_id, "display_name": display_name}
        email = safe_text(data.get("user_email"), 254)
        if email:
            result["email"] = email
        return result

    async def fetch_slicer_config(self) -> dict[str, Any]:
        """Return the bounded native envelope; profile parsing is separate."""
        return await self.request("POST", SLICER_CONFIG_PATH)

    async def download_config(self, url: str) -> bytes:
        """Read only the object URL returned by this session's latest config read.

        Storage is a separate authority: no Cloud headers, cookies, HTTP auth,
        or redirects can cross into it, including with an injected HTTP client.
        The presigned upload URL is never used.
        """
        if self._closed:
            raise AnycubicCloudError("Anycubic Cloud session is closed.", code="session_closed")
        if not isinstance(url, str) or url != self._config_download_url or len(url) > 16_384:
            raise AnycubicCloudError("Anycubic configuration download was not authorized.", code="download_refused")
        try:
            parsed = urlsplit(url)
            valid = (
                url.isascii()
                and all(33 <= ord(char) <= 126 for char in url)
                and parsed.scheme == "https"
                and parsed.hostname == CONFIG_DOWNLOAD_HOST
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and bool(parsed.path.strip("/"))
            )
        except ValueError:
            valid = False
        if not valid:
            raise AnycubicCloudError("Anycubic configuration download was not authorized.", code="download_refused")
        client = self._http_client()
        # Build independently: AsyncClient.build_request()/stream() would merge
        # account cookies and default XX-Token/Authorization headers here.
        request = httpx.Request(
            "GET",
            url,
            headers={"Accept": "application/zip, application/octet-stream", "User-Agent": _USER_AGENT},
            extensions={"timeout": httpx.Timeout(15.0, connect=5.0).as_dict()},
        )
        try:
            async with asyncio.timeout(_DOWNLOAD_DEADLINE_SECONDS):
                response = await client.send(request, stream=True, follow_redirects=False, auth=None)
                try:
                    if response.status_code >= 500 or response.status_code in (408, 425, 429):
                        raise AnycubicCloudUnavailableError()
                    if response.status_code != 200:
                        raise AnycubicCloudError(
                            "Anycubic configuration download was refused.", code="download_refused"
                        )
                    return await _response_bytes(response, MAX_CONFIG_DOWNLOAD_BYTES)
                finally:
                    await response.aclose()
        except (httpx.HTTPError, TimeoutError):
            raise AnycubicCloudUnavailableError() from None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request(method, path, params=params, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: Mapping[str, str | int] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise AnycubicCloudError("Anycubic Cloud session is closed.", code="session_closed")
        if not isinstance(method, str) or not isinstance(path, str) or (method, path) not in _ALLOWED_ENDPOINTS:
            raise AnycubicCloudError("This Anycubic Cloud operation is not supported.", code="operation_unsupported")
        credential = token if token is not None else self._token
        if credential is None:
            raise AnycubicCloudAuthError()
        exchange = path == TOKEN_EXCHANGE_PATH
        if exchange and (
            self._region != "global"
            or method != "POST"
            or params
            or json != {"device_type": "pcf", "access_token": credential}
        ):
            raise AnycubicCloudError("Invalid Anycubic token exchange.", code="invalid_request")
        if params is not None:
            if not isinstance(params, Mapping) or len(params) > 32:
                raise AnycubicCloudError("Invalid Anycubic Cloud query.", code="invalid_request")
            for key, value in params.items():
                if (
                    not isinstance(key, str)
                    or not 1 <= len(key) <= 64
                    or not isinstance(value, (str, int))
                    or isinstance(value, bool)
                    or len(str(value)) > 4096
                ):
                    raise AnycubicCloudError("Invalid Anycubic Cloud query.", code="invalid_request")
        if path in (USER_INFO_PATH, SLICER_CONFIG_PATH) and (params or json is not None):
            raise AnycubicCloudError("Invalid Anycubic Cloud read query.", code="invalid_request")
        if method == "GET" and json is not None:
            raise AnycubicCloudError("Invalid Anycubic Cloud read request.", code="invalid_request")
        content = None
        if json is not None:
            try:
                if not isinstance(json, dict):
                    raise ValueError("object required")
                content = json_module.dumps(json, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
            except (TypeError, ValueError, RecursionError):
                raise AnycubicCloudError("Invalid Anycubic Cloud request.", code="invalid_request") from None
            if len(content) > MAX_REQUEST_BYTES:
                raise AnycubicCloudError("Anycubic Cloud request is too large.", code="request_too_large")
        if path == SLICER_CONFIG_PATH:
            self._config_download_url = None
        client = self._http_client()
        headers = _headers(credential, self._region)
        if exchange:
            headers.pop("XX-Token")
        try:
            async with (
                asyncio.timeout(_REQUEST_DEADLINE_SECONDS),
                client.stream(
                    method,
                    CLOUD_ORIGINS[self._region] + API_PREFIX + path,
                    headers=headers,
                    params=params,
                    content=content,
                    follow_redirects=False,
                    timeout=httpx.Timeout(15.0, connect=5.0),
                ) as response,
            ):
                if response.status_code >= 500 or response.status_code in (408, 425, 429):
                    raise AnycubicCloudUnavailableError()
                if 300 <= response.status_code < 400:
                    raise AnycubicCloudError("Anycubic Cloud redirected the request.", code="redirect_refused")
                payload = await _response_bytes(response, MAX_RESPONSE_BYTES)
                status = response.status_code
        except (httpx.HTTPError, TimeoutError):
            raise AnycubicCloudUnavailableError() from None
        try:
            body = json_module.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise AnycubicCloudError("Anycubic Cloud returned an invalid response.", code="invalid_response") from None
        if not isinstance(body, dict):
            raise AnycubicCloudError("Anycubic Cloud returned an invalid response.", code="invalid_response")
        message = body.get("msg")
        if isinstance(message, str):
            normalized = message.strip().lower()
            if normalized == "request error" or "请求过于频繁" in message:
                raise AnycubicCloudUnavailableError()
            if normalized in {"user does not exist", "login information has expired. please login again."}:
                raise AnycubicCloudAuthError()
        if status in (401, 403):
            # An edge/interstitial response is not a verdict on stored tokens.
            raise AnycubicCloudUnavailableError()
        if status != 200:
            raise AnycubicCloudError("Anycubic Cloud refused the request.", code="request_refused")
        if "data" not in body:
            raise AnycubicCloudError("Anycubic Cloud returned an invalid response.", code="invalid_response")
        if type(body.get("code")) is not int or body["code"] != 1:
            if path in (USER_INFO_PATH, TOKEN_EXCHANGE_PATH) and body["data"] is None:
                raise AnycubicCloudAuthError()
            raise AnycubicCloudError("Anycubic Cloud refused the request.", code="request_refused")
        if path == SLICER_CONFIG_PATH and isinstance(body["data"], dict):
            config_url = body["data"].get("url")
            if isinstance(config_url, str):
                self._config_download_url = config_url
        return body

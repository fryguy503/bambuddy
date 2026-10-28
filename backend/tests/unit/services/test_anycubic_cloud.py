"""The optional cloud service admits account/profile reads, never printer control."""

import asyncio
import base64
import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.app.services.anycubic_cloud import (
    API_PREFIX,
    CONFIG_DOWNLOAD_HOST,
    MAX_CONFIG_DOWNLOAD_BYTES,
    MAX_RESPONSE_BYTES,
    SLICER_CONFIG_PATH,
    TOKEN_EXCHANGE_PATH,
    USER_INFO_PATH,
    AnycubicCloudAuthError,
    AnycubicCloudError,
    AnycubicCloudService,
    AnycubicCloudUnavailableError,
)

pytestmark = pytest.mark.unit
TOKEN = "fixture-cloud-user-token"
CONFIG_URL = f"https://{CONFIG_DOWNLOAD_HOST}/fixture/profiles.zip?X-Amz-Signature=fixture"


def _account():
    return {
        "code": 1,
        "msg": "success",
        "data": {
            "id": 42,
            "user_nickname": "Workshop",
            "user_email": "fixture@example.test",
            "password": "server-password-must-not-escape",
            "message_key": "server-message-key-must-not-escape",
            "token": "server-token-must-not-escape",
            "last_login_ip": "192.0.2.1",
        },
    }


def _slicer_token():
    payload = base64.urlsafe_b64encode(json.dumps({"tokenType": "access-token"}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.fixture-signature"


@pytest.mark.asyncio
async def test_slicer_access_token_is_exchanged_without_xx_token_then_identity_is_validated():
    source = _slicer_token()
    issued = "fixture-exchanged-user-token"
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.host == "cloud-universe.anycubic.com"
        if request.url.path == API_PREFIX + TOKEN_EXCHANGE_PATH:
            assert request.method == "POST"
            assert "XX-Token" not in request.headers
            assert json.loads(request.content) == {"device_type": "pcf", "access_token": source}
            return httpx.Response(200, json={"code": 1, "data": {"token": issued}})
        assert request.headers["XX-Token"] == issued
        return httpx.Response(200, json=_account())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(source, client=client)
        identity = await service.validate_token()
        assert identity["user_id"] == "42"
        assert service.account_token == issued
        assert source not in repr(service)
        assert issued not in repr(identity)
    assert calls == [API_PREFIX + TOKEN_EXCHANGE_PATH, API_PREFIX + USER_INFO_PATH]


@pytest.mark.asyncio
async def test_china_token_is_not_exchanged_or_sent_to_another_region():
    source = _slicer_token()

    def handler(request):
        assert request.url.host == "cloud-platform.anycubicloud.com"
        assert request.url.path == API_PREFIX + USER_INFO_PATH
        assert request.headers["XX-Token"] == source
        return httpx.Response(200, json=_account())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await AnycubicCloudService(source, "china", client=client).validate_token()


@pytest.mark.asyncio
async def test_rejected_exchange_never_accepts_unverified_jwt_identity():
    def handler(request):
        assert request.url.path == API_PREFIX + TOKEN_EXCHANGE_PATH
        return httpx.Response(200, json={"code": 0, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(_slicer_token(), client=client)
        with pytest.raises(AnycubicCloudAuthError):
            await service.validate_token()
        with pytest.raises(AnycubicCloudAuthError):
            _ = service.account_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("region", "host", "is_cn"),
    [("global", "cloud-universe.anycubic.com", "0"), ("china", "cloud-platform.anycubicloud.com", "1")],
)
async def test_token_validation_uses_selected_region_and_returns_identity_whitelist(region, host, is_cn):
    def handler(request):
        assert request.url.scheme == "https"
        assert request.url.host == host
        assert request.url.path == API_PREFIX + USER_INFO_PATH
        assert request.headers["XX-Token"] == TOKEN
        assert request.headers["XX-IS-CN"] == is_cn
        return httpx.Response(200, json=_account())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(TOKEN, region, client=client)
        assert await service.validate_token() == {
            "user_id": "42",
            "display_name": "Workshop",
            "email": "fixture@example.test",
        }
        assert TOKEN not in repr(service)


@pytest.mark.asyncio
async def test_native_public_signature_regression_and_version_match():
    def handler(request):
        assert request.headers["XX-Nonce"] == "589e501d-c930-4b31-bc44-b326df325250"
        assert request.headers["XX-Timestamp"] == "1759050000000"
        assert request.headers["XX-Version"] == "V3.0.0"
        assert request.headers["XX-Device-Type"] == "pcf"
        assert request.headers["XX-Signature"] == "9F9744587D5E3488BA57DC155619EF45"
        assert "Bambuddy" in request.headers["User-Agent"]
        return httpx.Response(200, json=_account())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with (
            patch("backend.app.services.anycubic_cloud.time.time_ns", return_value=1759050000000000000),
            patch(
                "backend.app.services.anycubic_cloud.uuid.uuid4",
                return_value=uuid.UUID("589e501d-c930-4b31-bc44-b326df325250"),
            ),
        ):
            await AnycubicCloudService(TOKEN, client=client).validate_token()


@pytest.mark.asyncio
async def test_signatures_are_fresh_for_each_request():
    nonces = []

    def handler(request):
        nonces.append(request.headers["XX-Nonce"])
        assert uuid.UUID(nonces[-1]).version == 4
        return httpx.Response(200, json=_account())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        await service.validate_token()
        await service.validate_token()
    assert len(set(nonces)) == 2


@pytest.mark.asyncio
async def test_config_read_is_native_empty_post_and_preserves_envelope():
    envelope = {"code": 1, "msg": "success", "pageData": {}, "data": {"name": "profile.json", "url": "fixture"}}

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == API_PREFIX + SLICER_CONFIG_PATH
        assert request.url.query == b""
        assert request.content == b""
        assert request.headers["Content-Type"] == "application/json"
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await AnycubicCloudService(TOKEN, client=client).fetch_slicer_config() == envelope


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/work/operation/sendOrder"),
        ("POST", "/v3/user/setSlicerConfig"),
        ("GET", "/v3/user/getSlicerConfig"),
        ("GET", "/v2/printer/info"),
        ("GET", "https://attacker.example/user/profile/userInfo"),
        ("GET", "/user/profile/userInfo?forward=https://attacker.example"),
        ("GET", "/user/profile/userInfo/../sendOrder"),
        ("DELETE", USER_INFO_PATH),
        ("GET", [USER_INFO_PATH]),
    ],
)
async def test_unlisted_endpoints_never_reach_transport(method, path):
    handler = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).request(method, path)
    assert error.value.code == "operation_unsupported"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "bad\r\nXX-Token: injected", " spaced ", "☃", "x" * 16385])
async def test_bad_tokens_are_rejected_before_transport(token):
    handler = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(client=client)
        with pytest.raises(AnycubicCloudError) as error:
            await service.connect_token(token)
    assert error.value.code == "invalid_token_format"
    handler.assert_not_awaited()


@pytest.mark.parametrize("region", ["international", "https://attacker.example", "global/../china", None, {}])
def test_regions_cannot_be_arbitrary_origins(region):
    with pytest.raises(AnycubicCloudError) as error:
        AnycubicCloudService(TOKEN, region)
    assert error.value.code == "invalid_region"


@pytest.mark.asyncio
async def test_redirects_are_not_followed_even_with_redirecting_injected_client():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example/steal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert error.value.code == "redirect_refused"
    assert len(requests) == 1
    assert requests[0].url.host == "cloud-universe.anycubic.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
async def test_transient_http_errors_are_not_credentials_errors(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text=TOKEN))) as client:
        with pytest.raises(AnycubicCloudUnavailableError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert not isinstance(error.value, AnycubicCloudAuthError)
    assert TOKEN not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"code": 0, "msg": "request error", "data": None},
        {"code": 0, "msg": "请求过于频繁。请稍后再试", "data": None},
    ],
)
async def test_native_transient_messages_do_not_invalidate_token(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(AnycubicCloudUnavailableError):
            await AnycubicCloudService(TOKEN, client=client).validate_token()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"code": 0, "msg": "User does not exist", "data": None},
        {"code": 0, "msg": "Login information has expired. Please login again."},
        {"code": 1, "data": None},
        {"code": 1, "data": {}},
    ],
)
async def test_definite_credentials_rejections_are_distinct(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(AnycubicCloudAuthError):
            await AnycubicCloudService(TOKEN, client=client).validate_token()


@pytest.mark.asyncio
async def test_failed_token_replacement_preserves_previous_credential():
    seen = []

    def handler(request):
        seen.append(request.headers["XX-Token"])
        return httpx.Response(200, json=_account() if seen[-1] == TOKEN else {"code": 1, "data": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        with pytest.raises(AnycubicCloudAuthError):
            await service.connect_token("refused-replacement")
        await service.validate_token()
    assert seen == ["refused-replacement", TOKEN]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [b"<html>account secret</html>", b'{"code":1,"data":{},"data":null}', b'{"code":1,"data":NaN}', b"[]"],
)
async def test_invalid_and_ambiguous_json_has_fixed_safe_error(content):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))
    ) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert error.value.code == "invalid_response"
    assert "account secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("announced", [True, False])
async def test_response_size_is_bounded_with_or_without_content_length(announced):
    response = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    if not announced:
        del response.headers["content-length"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert error.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_transport_error_cannot_disclose_request_exception_contents():
    def handler(_):
        raise httpx.ConnectError(f"credential={TOKEN}, secret response body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnycubicCloudUnavailableError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert TOKEN not in str(error.value)
    assert "secret response" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_missing_token_and_closed_session_never_request_network():
    handler = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(client=client)
        with pytest.raises(AnycubicCloudAuthError):
            await service.validate_token()
        await service.close()
        with pytest.raises(AnycubicCloudError) as error:
            await service.connect_token(TOKEN)
        assert error.value.code == "session_closed"
        assert not client.is_closed  # injected clients remain caller-owned
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_query_does_not_admit_caller_parameters():
    handler = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).request("GET", USER_INFO_PATH, params={"id": 43})
    assert error.value.code == "invalid_request"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_drops_control_characters_and_credential_echoes():
    body = _account()
    body["data"]["user_nickname"] = TOKEN
    body["data"]["user_email"] = "secret\r\nemail@example.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        assert await AnycubicCloudService(TOKEN, client=client).validate_token() == {
            "user_id": "42",
            "display_name": "42",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [True, 0, -1, 1.5, "https://attacker.example", "9" * 33, [42]])
async def test_invalid_account_ids_are_protocol_faults_not_credentials_verdicts(user_id):
    body = _account()
    body["data"]["id"] = user_id
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert error.value.code == "invalid_response"
    assert not isinstance(error.value, AnycubicCloudAuthError)


@pytest.mark.asyncio
async def test_boolean_success_code_cannot_admit_malformed_envelope():
    body = _account()
    body["code"] = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert error.value.code == "request_refused"


@pytest.mark.asyncio
async def test_unclassified_cloud_error_body_cannot_escape_to_the_caller():
    body = {"code": 0, "msg": TOKEN + " private vendor response", "data": {"trace": "private"}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(AnycubicCloudError) as error:
            await AnycubicCloudService(TOKEN, client=client).validate_token()
    assert error.value.code == "request_refused"
    assert TOKEN not in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.asyncio
async def test_account_namespace_is_validated_and_region_bound():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_account()))
    ) as client:
        service = AnycubicCloudService(TOKEN, "china", client=client)
        with pytest.raises(AnycubicCloudAuthError):
            _ = service.account_namespace
        await service.validate_token()
        assert service.account_namespace == "anycubic:china:42"
        await service.close()
        with pytest.raises(AnycubicCloudAuthError):
            _ = service.account_namespace


@pytest.mark.asyncio
async def test_storage_download_never_inherits_cloud_or_injected_credentials():
    def handler(request):
        if request.url.host == CONFIG_DOWNLOAD_HOST:
            assert request.method == "GET"
            assert "XX-Token" not in request.headers
            assert "XX-Signature" not in request.headers
            assert "Authorization" not in request.headers
            assert "Cookie" not in request.headers
            return httpx.Response(200, content=b"zip-fixture")
        return httpx.Response(
            200,
            headers={"Set-Cookie": "cloud-cookie=secret; Path=/"},
            json={"code": 1, "data": {"url": CONFIG_URL, "preSignUrl": "https://attacker.example/upload"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"XX-Token": "injected-token", "Authorization": "Bearer injected-key"},
        cookies={"session": "injected-cookie"},
        auth=("injected-user", "injected-password"),
    ) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        await service.fetch_slicer_config()
        assert await service.download_config(CONFIG_URL) == b"zip-fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://" + CONFIG_DOWNLOAD_HOST + "/profiles.zip",
        "https://" + CONFIG_DOWNLOAD_HOST + ".attacker.example/profiles.zip",
        "https://attacker.example/profiles.zip",
        "https://127.0.0.1/profiles.zip",
        "https://[::1]/profiles.zip",
        "https://" + CONFIG_DOWNLOAD_HOST + ":8443/profiles.zip",
        "https://user:secret@" + CONFIG_DOWNLOAD_HOST + "/profiles.zip",
        "https://" + CONFIG_DOWNLOAD_HOST + "/profiles.zip#fragment",
        "https://" + CONFIG_DOWNLOAD_HOST + "/profiles\r\n.zip",
        "https://" + CONFIG_DOWNLOAD_HOST + "/",
        CONFIG_URL + "x" * 16384,
    ],
)
async def test_config_download_rejects_unapproved_origins_even_when_vendor_returns_them(url):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 1, "data": {"url": url}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        await service.fetch_slicer_config()
        with pytest.raises(AnycubicCloudError) as error:
            await service.download_config(url)
    assert error.value.code == "download_refused"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_only_latest_config_read_url_is_admitted_and_upload_url_is_never_used():
    calls = 0

    def handler(_):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"code": 1, "data": {"url": CONFIG_URL + str(calls), "preSignUrl": CONFIG_URL + "upload"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        with pytest.raises(AnycubicCloudError, match="not authorized"):
            await service.download_config(CONFIG_URL)
        await service.fetch_slicer_config()
        await service.fetch_slicer_config()
        for refused in (CONFIG_URL + "1", CONFIG_URL + "upload"):
            with pytest.raises(AnycubicCloudError, match="not authorized"):
                await service.download_config(refused)
    assert calls == 2


@pytest.mark.asyncio
async def test_storage_redirect_never_sends_credentials_or_follows_location():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == CONFIG_DOWNLOAD_HOST:
            return httpx.Response(307, headers={"Location": "https://attacker.example/config.zip"})
        return httpx.Response(200, json={"code": 1, "data": {"url": CONFIG_URL}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        await service.fetch_slicer_config()
        with pytest.raises(AnycubicCloudError) as error:
            await service.download_config(CONFIG_URL)
    assert error.value.code == "download_refused"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_configuration_download_is_bounded_to_sixteen_mib():
    def handler(request):
        if request.url.host == CONFIG_DOWNLOAD_HOST:
            return httpx.Response(200, headers={"Content-Length": str(MAX_CONFIG_DOWNLOAD_BYTES + 1)})
        return httpx.Response(200, json={"code": 1, "data": {"url": CONFIG_URL}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        await service.fetch_slicer_config()
        with pytest.raises(AnycubicCloudError) as error:
            await service.download_config(CONFIG_URL)
    assert error.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_cloud_request_has_an_overall_deadline(monkeypatch):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(60)
            yield b"{}"

    monkeypatch.setattr("backend.app.services.anycubic_cloud._REQUEST_DEADLINE_SECONDS", 0.01)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream()))
    ) as client:
        with pytest.raises(AnycubicCloudUnavailableError):
            await AnycubicCloudService(TOKEN, client=client).validate_token()


@pytest.mark.asyncio
async def test_storage_download_has_an_overall_deadline(monkeypatch):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(60)
            yield b"zip"

    def handler(request):
        if request.url.host == CONFIG_DOWNLOAD_HOST:
            return httpx.Response(200, stream=SlowStream())
        return httpx.Response(200, json={"code": 1, "data": {"url": CONFIG_URL}})

    monkeypatch.setattr("backend.app.services.anycubic_cloud._DOWNLOAD_DEADLINE_SECONDS", 0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = AnycubicCloudService(TOKEN, client=client)
        await service.fetch_slicer_config()
        with pytest.raises(AnycubicCloudUnavailableError):
            await service.download_config(CONFIG_URL)

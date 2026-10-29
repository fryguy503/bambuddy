"""Official website contracts with synthetic tokens/files; no account requests."""

import json
from unittest.mock import patch

import httpx
import pytest

from backend.app.services.anycubic_cloud_credentials import AnycubicCredentials
from backend.app.services.model_providers import registry
from backend.app.services.model_providers.base import (
    ProviderAuthError,
    ProviderForbiddenError,
    ProviderResourceRef,
    ProviderUnavailableError,
    ProviderUrlError,
)
from backend.app.services.model_providers.makeronline import MakerOnlineService, _storage_url, makeronline_provider

SSO = "synthetic-sso-secret"
WORKBENCH = "different-workbench-secret"
REF = ProviderResourceRef("makeronline", "342885", "236543")
URL = "https://acop-prod.s3.us-east-2.amazonaws.com/path/project.3mf?signature=synthetic"


def model():
    record = {
        "id": 1,
        "file_id": 236543,
        "file_name": "Project.3mf",
        "key": "native-key",
        "simple_url": ["path/project.config", "path/project.3mf"],
    }
    return {
        "id": 342885,
        "title": "Example",
        "license": 1,
        "secret": SSO,
        "files": [],
        "print_profiles": [{"id": 236543, "printer_label": "Kobra S1", "files": [record, dict(record, id=2)]}],
    }


@pytest.mark.parametrize(
    "url",
    ["https://www.makeronline.com/en/model/example/342885.html", "makeronline.com/en/model/342885.html?share=test"],
)
def test_registered_provider_canonicalizes_real_model_urls(url):
    assert registry.find_for_url(url) is makeronline_provider
    ref = makeronline_provider.parse_url(url)
    assert ref.external_id == "342885"
    assert makeronline_provider.canonical_url(REF) == "https://www.makeronline.com/en/model/342885.html#file-236543"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/en/model/342885.html",
        "http://makeronline.com/en/model/342885.html",
        "https://user:secret@makeronline.com/en/model/342885.html",
        "https://makeronline.com:444/en/model/342885.html",
        "https://makeronline.com.evil.test/en/model/342885.html",
        "https://makeronline.com/en/model/-1.html",
    ],
)
def test_model_url_cannot_select_an_arbitrary_endpoint(url):
    with pytest.raises(ProviderUrlError):
        makeronline_provider.parse_url(url)


@pytest.mark.asyncio
async def test_resolve_and_download_use_shared_sso_without_exposing_private_paths():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == SSO
        assert WORKBENCH not in str(request.headers)
        if request.method == "GET":
            assert request.url.path == "/api/mold/detail"
            return httpx.Response(200, json={"code": 0, "data": model()})
        assert request.url.path == "/api/file/get-download-url"
        assert json.loads(request.content) == {"path": ["path/project.config", "path/project.3mf"]}
        return httpx.Response(200, json={"code": 0, "data": {"path/project.3mf": URL}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MakerOnlineService(AnycubicCredentials(token=WORKBENCH, makeronline_token=SSO), client=client)
        result = await service.resolve(REF)
        assert len(result.instances) == 1  # Two plates, one complete project.
        public = json.dumps({"design": result.design, "instances": result.instances})
        assert all(secret not in public for secret in [SSO, WORKBENCH, "native-key", "path/project"])
        info = await service.get_download(REF)
        assert info.url == URL and info.suggested_filename == "Project.3mf"
        assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credentials",
    [
        AnycubicCredentials(token=WORKBENCH),
        AnycubicCredentials(token=WORKBENCH, makeronline_token=SSO, region="china"),
        AnycubicCredentials(token=WORKBENCH, makeronline_token=SSO, invalid=True),
    ],
)
async def test_missing_or_wrong_authority_never_sends_a_cloud_user_token(credentials):
    def handler(_request):
        pytest.fail("A missing Global SSO must not cause a download API request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MakerOnlineService(credentials, client=client)
        assert not (await service.get_status(None)).can_download
        with pytest.raises(ProviderAuthError):
            await service.get_download(REF)


@pytest.mark.asyncio
async def test_storage_uses_separate_client_with_no_account_headers_or_cookies():
    def handler(request):
        assert request.url.host == "acop-prod.s3.us-east-2.amazonaws.com"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=b"PK\x03\x04synthetic-3mf")

    storage = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = MakerOnlineService(AnycubicCredentials(token=WORKBENCH, makeronline_token=SSO))
    from backend.app.services.model_providers.base import ProviderDownloadInfo

    with patch("backend.app.services.model_providers.makeronline.httpx.AsyncClient", return_value=storage):
        result = await service.download(ProviderDownloadInfo(REF, URL, "Project.3mf"))
    assert result.file_bytes.startswith(b"PK")
    await service.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://acop-prod.s3.us-east-2.amazonaws.com/file",
        "https://127.0.0.1/file",
        "https://acop-prod.s3.us-east-2.amazonaws.com.evil.test/file",
        "https://acop-prod.s3.us-east-2.amazonaws.com:444/file",
        "https://secret@acop-prod.s3.us-east-2.amazonaws.com/file",
    ],
)
def test_presigned_download_is_limited_to_observed_vendor_storage(url):
    with pytest.raises(ProviderUnavailableError):
        _storage_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,error",
    [
        (302, {}, ProviderUnavailableError),
        (403, {}, ProviderAuthError),
        (200, {"code": 100, "message": SSO}, ProviderForbiddenError),
    ],
)
async def test_failures_are_fixed_messages_and_never_follow_redirects(status, body, error):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body, headers={"location": "https://evil.test"})
        )
    ) as client:
        service = MakerOnlineService(AnycubicCredentials(makeronline_token=SSO), client=client)
        with pytest.raises(error) as failure:
            await service.resolve(REF)
        assert SSO not in str(failure.value)

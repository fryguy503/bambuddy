"""Plugin storage and model browsing use synthetic accounts and HTTP only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import encryption
from backend.app.core.auth import ApiKeyActor
from backend.app.models.api_key import APIKey
from backend.app.models.settings import Settings
from backend.app.models.user import User
from plugins.anycubic.payload import cloud_routes, cloud_storage
from plugins.anycubic.payload.cloud import AnycubicCloudError, MakerOnlineCatalog, cloud_operation, parse_model_url


@pytest.fixture
async def cloud_db(monkeypatch):
    # These tests never open the application's configured database or key file.
    monkeypatch.setattr(encryption, "_fernet_instance", Fernet(Fernet.generate_key()))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Settings.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cloud_routes._profile_cache.clear()
    try:
        async with sessions() as session:
            yield session
    finally:
        cloud_routes._profile_cache.clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_credentials_are_encrypted_and_scoped_to_owner_without_core_columns(cloud_db):
    alice, bob = SimpleNamespace(id=21), SimpleNamespace(id=22)
    for owner, token in ((alice, "alice-cloud-secret"), (bob, "bob-cloud-secret"), (None, "workspace-cloud-secret")):
        await cloud_storage.store_credentials(cloud_db, owner, expected=None, token=token, user_id="42")
        loaded = await cloud_storage.load_credentials(cloud_db, owner)
        assert loaded.token == token
        assert token not in repr(loaded)
    rows = (await cloud_db.scalars(select(Settings))).all()
    assert len(rows) == 3
    assert all(row.value.startswith("fernet:") for row in rows)
    assert all("cloud-secret" not in row.value for row in rows)
    assert all(row.key.startswith("printer_plugin:anycubic:cloud:owner:") for row in rows)


@pytest.mark.asyncio
async def test_ciphertext_is_bound_to_its_owner_and_cannot_be_copied(cloud_db):
    alice, bob = SimpleNamespace(id=31), SimpleNamespace(id=32)
    await cloud_storage.store_credentials(cloud_db, alice, expected=None, token="alice-secret")
    ciphertext = await cloud_storage.read_stored(cloud_db, alice)
    cloud_db.add(Settings(key=cloud_storage.owner_key(bob), value=ciphertext))
    await cloud_db.commit()
    with pytest.raises(HTTPException) as rejected:
        await cloud_storage.load_credentials(cloud_db, bob)
    assert rejected.value.status_code == 503
    assert "alice-secret" not in str(rejected.value)


@pytest.mark.asyncio
async def test_logout_fences_inflight_connect_and_stale_reads(cloud_db):
    owner = SimpleNamespace(id=41)
    await cloud_storage.store_credentials(cloud_db, owner, expected=None, token="original-secret")
    old = await cloud_storage.load_credentials(cloud_db, owner)
    await cloud_storage.store_credentials(cloud_db, owner, expected=old.stored)
    assert (await cloud_storage.load_credentials(cloud_db, owner)).token is None
    with pytest.raises(HTTPException) as stale:
        await cloud_storage.store_credentials(cloud_db, owner, expected=old.stored, token="late-connect-secret")
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as stale_read:
        await cloud_storage.ensure_current(cloud_db, owner, old)
    assert stale_read.value.status_code == 409
    assert (await cloud_storage.load_credentials(cloud_db, owner)).token is None


@pytest.mark.asyncio
async def test_no_plaintext_fallback_but_disconnect_survives_lost_encryption_key(cloud_db, monkeypatch):
    owner = SimpleNamespace(id=51)
    monkeypatch.setattr(cloud_storage, "mfa_encrypt", lambda value: value)
    with pytest.raises(HTTPException) as unavailable:
        await cloud_storage.store_credentials(cloud_db, owner, expected=None, token="must-not-save-in-plaintext")
    assert unavailable.value.status_code == 503
    assert await cloud_storage.read_stored(cloud_db, owner) is None
    cloud_db.add(Settings(key=cloud_storage.owner_key(owner), value="fernet:unreadable-old-value"))
    await cloud_db.commit()
    with pytest.raises(HTTPException):
        await cloud_storage.load_credentials(cloud_db, owner)
    await cloud_storage.store_credentials(cloud_db, owner, expected=await cloud_storage.read_stored(cloud_db, owner))
    assert (await cloud_storage.load_credentials(cloud_db, owner)).token is None


@pytest.mark.asyncio
async def test_package_routes_connect_and_disconnect_only_the_current_owner(cloud_db, monkeypatch):
    owner, other = SimpleNamespace(id=61), SimpleNamespace(id=62)
    service = SimpleNamespace(
        validate_token=AsyncMock(return_value={"user_id": "42", "display_name": "Workshop"}),
        account_token="verified-workbench-token",
        makeronline_token="verified-sso-token",
        close=AsyncMock(),
    )
    monkeypatch.setattr(cloud_routes, "AnycubicCloudService", lambda **kwargs: service)
    connected = await cloud_routes.connect_token(
        cloud_routes.AnycubicTokenConnect(token="candidate-token"), cloud_db, owner
    )
    assert connected.connected and connected.user_id == "42"
    assert all(
        secret not in connected.model_dump_json()
        for secret in ("candidate-token", "verified-workbench-token", "verified-sso-token")
    )
    assert not (await cloud_routes.status(cloud_db, other)).connected
    await cloud_routes.logout(cloud_db, owner)
    assert not (await cloud_routes.status(cloud_db, owner)).connected
    service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cloud_api_keys_require_an_owner_and_cloud_scope():
    owner = User(id=71, username="owner", role="admin", groups=[])
    resolve = cloud_routes.cloud_caller().dependency
    for account_owner, allowed in ((None, False), (owner, False), (owner, True)):
        key = APIKey(user_id=owner.id if account_owner else None, can_access_cloud=allowed, can_read_status=True)
        actor = ApiKeyActor(key, account_owner)
        if account_owner is not None and allowed:
            assert await resolve(_permission=None, actor=actor) is owner
        else:
            with pytest.raises(HTTPException) as rejected:
                await resolve(_permission=None, actor=actor)
            assert rejected.value.status_code == 403
    assert await resolve(_permission=owner, actor=owner) is owner
    assert await resolve(_permission=None, actor=None) is None


@pytest.mark.asyncio
async def test_makeronline_metadata_uses_its_own_authority_and_only_public_fields():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://www.makeronline.com/api/mold/detail?id=123"
        assert request.headers["Authorization"] == "maker-sso-secret"
        assert "XX-Token" not in request.headers and "Cookie" not in request.headers
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": 123,
                    "title": "Fixture model",
                    "license": "CC-BY",
                    "files": [{"id": 7, "filename": "model.3mf", "key": "private-key", "simple_url": ["private/path"]}],
                    "token": "response-secret",
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"XX-Token": "other-account-secret"},
        cookies={"session": "secret"},
    ) as client:
        catalog = MakerOnlineCatalog("maker-sso-secret", client=client)
        result = await cloud_operation("model", catalog=catalog, model_id="123")
        await catalog.close()
    assert len(requests) == 1
    assert result["files"] == [{"id": "7", "filename": "model.3mf", "printer": ""}]
    assert "secret" not in str(result) and "private" not in str(result)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/model/1.html",
        "https://makeronline.com.evil/model/1.html",
        "http://makeronline.com/model/1.html",
        "https://owner:secret@makeronline.com/model/1.html",
    ],
)
def test_model_url_parser_rejects_other_authorities(url):
    with pytest.raises(AnycubicCloudError):
        parse_model_url(url)


def test_model_url_parser_resolves_only_a_model_identity():
    assert parse_model_url("https://www.makeronline.com/en/model/fixture-model/123.html") == "123"


@pytest.mark.asyncio
async def test_cloud_hook_refuses_commands_and_writes_before_side_effects():
    service = SimpleNamespace(request=AsyncMock())
    for operation in ("job.start", "sendOrder", "upload", "delete", "setSlicerConfig"):
        with pytest.raises(AnycubicCloudError):
            await cloud_operation(operation, service=service)
    service.request.assert_not_awaited()

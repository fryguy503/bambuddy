"""Unlinking cloud credentials must not require a working encryption key."""

import asyncio
import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.app.models.user import User

ROOT = "/api/v1/anycubic-cloud"
SECRET = "synthetic-private-cloud-user-token"
SERVICE = "backend.app.api.routes.anycubic_cloud.AnycubicCloudService"
CREDENTIALS = "backend.app.services.anycubic_cloud_credentials"
SETTING_KEY = "anycubic_cloud_token"

pytestmark = pytest.mark.integration


@pytest.fixture
def cloud_service():
    with patch(SERVICE) as constructor:
        service = constructor.return_value
        service.validate_token = AsyncMock(return_value={"user_id": "42", "display_name": "Fixture Workshop"})
        service.account_token = SECRET
        service.close = AsyncMock()
        yield service


@pytest.fixture(params=["global", "user"])
async def connection(request, async_client):
    if request.param == "global":
        return {}
    configured = await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": "marker-admin", "admin_password": "FixtureAdminPass1!"},
    )
    assert configured.status_code == 200
    login = await async_client.post(
        "/api/v1/auth/login", json={"username": "marker-admin", "password": "FixtureAdminPass1!"}
    )
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["access_token"]}


async def _stored(db, headers):
    if headers:
        query = select(User.anycubic_cloud_token).where(User.username == "marker-admin")
    else:
        query = select(Settings.value).where(Settings.key == SETTING_KEY)
    return (await db.execute(query)).scalar_one_or_none()


async def _connect(client, headers):
    return await client.post(ROOT + "/token", headers=headers, json={"token": SECRET, "region": "global"})


async def test_logout_of_encrypted_account_works_when_encryption_key_is_unavailable(
    async_client, db_session, cloud_service, connection
):
    assert (await _connect(async_client, connection)).status_code == 200
    assert (await _stored(db_session, connection)).startswith("fernet:")

    with (
        patch(CREDENTIALS + ".mfa_encrypt", side_effect=lambda body: body) as encrypt,
        patch(CREDENTIALS + ".mfa_decrypt", side_effect=RuntimeError("Synthetic missing encryption key")) as decrypt,
    ):
        unreadable = await async_client.get(ROOT + "/status", headers=connection)
        assert unreadable.status_code == 503
        response = await async_client.post(ROOT + "/logout", headers=connection)
        assert response.status_code == 200
        state = await async_client.get(ROOT + "/status", headers=connection)
        assert state.status_code == 200
        assert state.json()["connected"] is False
        assert state.json()["user_id"] is None
        assert state.json()["display_name"] is None
        assert SECRET not in state.text
    encrypt.assert_not_called()
    assert decrypt.call_count == 1  # only the pre-logout status read
    marker = await _stored(db_session, connection)
    assert re.fullmatch(r"anycubic-disconnected:v1:[0-9a-f]{32}", marker)
    assert SECRET not in marker


async def test_unavailable_encryption_key_never_saves_plaintext_account_credentials(
    async_client, db_session, cloud_service, connection
):
    with patch(CREDENTIALS + ".mfa_encrypt", side_effect=lambda body: body):
        response = await _connect(async_client, connection)
    assert response.status_code == 503
    assert SECRET not in response.text
    assert await _stored(db_session, connection) is None
    state = await async_client.get(ROOT + "/status", headers=connection)
    assert state.status_code == 200 and state.json()["connected"] is False


async def test_logout_revision_prevents_late_login_after_key_unavailable_logout(
    async_client, db_session, cloud_service, connection
):
    validation_started = asyncio.Event()
    finish_validation = asyncio.Event()

    async def validate():
        validation_started.set()
        await finish_validation.wait()
        return {"user_id": "42", "display_name": "Fixture Workshop"}

    cloud_service.validate_token.side_effect = validate
    pending = asyncio.create_task(_connect(async_client, connection))
    try:
        await asyncio.wait_for(validation_started.wait(), timeout=5)
        with patch(CREDENTIALS + ".mfa_encrypt", side_effect=lambda body: body) as encrypt:
            assert (await async_client.post(ROOT + "/logout", headers=connection)).status_code == 200
        encrypt.assert_not_called()
        marker = await _stored(db_session, connection)
        assert re.fullmatch(r"anycubic-disconnected:v1:[0-9a-f]{32}", marker)
        # The key is usable again, but the old request's revision still cannot
        # replace the explicit logout in either per-user or Settings storage.
        finish_validation.set()
        response = await asyncio.wait_for(pending, timeout=5)
        assert response.status_code == 409
        assert await _stored(db_session, connection) == marker
        state = await async_client.get(ROOT + "/status", headers=connection)
        assert state.status_code == 200 and state.json()["connected"] is False
    finally:
        finish_validation.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "marker",
    [
        "anycubic-disconnected:v1:",
        "anycubic-disconnected:v1:" + "a" * 31,
        "anycubic-disconnected:v1:" + "a" * 33,
        "anycubic-disconnected:v1:" + "A" * 32,
        "anycubic-disconnected:v1:" + "g" * 32,
        "anycubic-disconnected:v1:" + "a" * 32 + "\n",
        "anycubic-disconnected:v2:" + "a" * 32,
        json.dumps({"token": SECRET, "region": "global", "user_id": "42"}),
    ],
)
async def test_malformed_markers_and_plaintext_credentials_are_not_admitted(async_client, db_session, marker):
    db_session.add(Settings(key=SETTING_KEY, value=marker))
    await db_session.commit()
    with patch(SERVICE) as constructor:
        response = await async_client.get(ROOT + "/status")
    assert response.status_code == 503
    assert SECRET not in response.text
    assert marker not in response.text
    constructor.assert_not_called()


async def test_repeated_logout_issues_distinct_nonsecret_revisions(async_client, db_session):
    assert (await async_client.post(ROOT + "/logout")).status_code == 200
    first = await _stored(db_session, {})
    assert (await async_client.post(ROOT + "/logout")).status_code == 200
    second = await _stored(db_session, {})
    assert first != second
    assert re.fullmatch(r"anycubic-disconnected:v1:[0-9a-f]{32}", first)
    assert re.fullmatch(r"anycubic-disconnected:v1:[0-9a-f]{32}", second)

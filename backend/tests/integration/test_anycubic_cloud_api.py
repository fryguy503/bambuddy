"""Cloud connection, private credentials and profile admission without live services."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.core.encryption import mfa_decrypt
from backend.app.models.settings import Settings
from backend.app.services.anycubic_cloud import AnycubicCloudAuthError, AnycubicCloudUnavailableError
from backend.app.services.anycubic_cloud_profiles import AnycubicProfileError

ROOT = "/api/v1/anycubic-cloud"
SECRET = "a-synthetic-private-anycubic-token"
PUBLIC = {"user_id": "account-1", "display_name": "Maker"}
SERVICE = "backend.app.api.routes.anycubic_cloud.AnycubicCloudService"
PROFILES = "backend.app.api.routes.anycubic_cloud.load_profiles"


@pytest.fixture(autouse=True)
def clear_provider_cache():
    from backend.app.api.routes.anycubic_cloud import _profile_cache

    _profile_cache.clear()
    yield
    _profile_cache.clear()


@pytest.fixture
def cloud_service():
    with patch(SERVICE) as constructor:
        service = constructor.return_value
        service.validate_token = AsyncMock(return_value=PUBLIC)
        service.account_token = SECRET
        service.makeronline_token = None
        service.account_namespace = "anycubic:global:account-1"
        service.close = AsyncMock()
        yield service


async def connect(client, token=SECRET):
    return await client.post(ROOT + "/token", json={"token": token, "region": "global"})


async def test_disconnected_status_does_not_contact_cloud(async_client):
    with patch(SERVICE) as constructor:
        response = await async_client.get(ROOT + "/status")
    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["auth_methods"] == ["token"]
    constructor.assert_not_called()


async def test_connection_validates_then_encrypts_token(async_client, db_session, cloud_service):
    response = await connect(async_client)
    assert response.status_code == 200
    assert response.json()["user_id"] == "account-1"
    assert response.json()["connected"] is True
    assert SECRET not in response.text
    raw = (await db_session.execute(select(Settings.value).where(Settings.key == "anycubic_cloud_token"))).scalar_one()
    assert raw.startswith("fernet:")
    assert SECRET not in raw
    assert json.loads(mfa_decrypt(raw))["token"] == SECRET
    cloud_service.validate_token.assert_awaited_once()
    cloud_service.close.assert_awaited_once()


async def test_slicer_login_stores_the_exchanged_user_token(async_client, db_session, cloud_service):
    cloud_service.account_token = "fixture-workbench-user-token"
    response = await connect(async_client, "fixture-slicer-access-token")
    assert response.status_code == 200
    raw = (await db_session.execute(select(Settings.value).where(Settings.key == "anycubic_cloud_token"))).scalar_one()
    assert json.loads(mfa_decrypt(raw))["token"] == "fixture-workbench-user-token"
    assert "fixture-slicer-access-token" not in raw
    assert "fixture-workbench-user-token" not in response.text


@pytest.mark.parametrize("token", [" ", "token\nsecond-line", "非ASCII", "token with spaces"])
async def test_invalid_token_format_is_rejected_without_network(async_client, token):
    with patch(SERVICE) as constructor:
        response = await connect(async_client, token)
    assert response.status_code == 422
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [{"token": SECRET + "\n" + SECRET}, {"token": SECRET, "region": SECRET}],
)
async def test_validation_errors_never_echo_credentials(async_client, body):
    with patch(SERVICE) as constructor:
        response = await async_client.post(ROOT + "/token", json=body)
    assert response.status_code == 422
    assert SECRET not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])
    constructor.assert_not_called()


@pytest.mark.parametrize("error,status", [(AnycubicCloudAuthError(), 401), (AnycubicCloudUnavailableError(), 502)])
async def test_failed_replacement_keeps_original_account(async_client, cloud_service, error, status):
    assert (await connect(async_client)).status_code == 200
    cloud_service.validate_token.side_effect = error
    response = await connect(async_client, "another-private-token")
    assert response.status_code == status
    assert "another-private-token" not in response.text
    state = (await async_client.get(ROOT + "/status")).json()
    assert state["connected"] is True
    assert state["user_id"] == "account-1"


def entries():
    return [
        {
            "setting_id": "ac-filament",
            "name": "PLA Matte",
            "type": "filament",
            "version": "1",
            "updated_time": 1700000000,
            "setting": {"name": "PLA Matte", "inherits": "Anycubic PLA @Kobra S1", "type": "filament"},
        },
        {
            "setting_id": "ac-machine",
            "name": "Kobra S1",
            "type": "printer",
            "setting": {
                "type": "machine",
                "printhost_password": "printer-secret",
                "print_host": "http://user:password@printer.local/",
            },
        },
        {"setting_id": "ac-unknown", "name": "Future kind", "type": "future-type", "setting": {"type": "future-type"}},
    ]


async def test_list_groups_unknown_types_and_caches_only_own_account(async_client, cloud_service):
    await connect(async_client)
    with patch(PROFILES, new_callable=AsyncMock, return_value=entries()) as fetch:
        response = await async_client.get(ROOT + "/profiles")
        assert response.status_code == 200
        data = response.json()
        assert [row["name"] for row in data["filament"]] == ["PLA Matte"]
        assert data["filament"][0]["updated_time"] == "1700000000"
        assert data["unsupported"][0]["type"] == "future-type"
        assert all("setting" not in row for row in data["printer"])
        assert (await async_client.get(ROOT + "/profiles")).status_code == 200
        assert fetch.await_count == 1
        assert (await async_client.get(ROOT + "/profiles?refresh=true")).status_code == 200
        assert fetch.await_count == 2


async def test_detail_preserves_settings_and_masks_connection_secrets(async_client, cloud_service):
    await connect(async_client)
    with patch(PROFILES, new_callable=AsyncMock, return_value=entries()):
        response = await async_client.get(ROOT + "/profiles/ac-machine")
        assert response.status_code == 200
        assert "printer-secret" not in response.text
        assert "user:password" not in response.text
        assert response.json()["setting"]["type"] == "machine"
        assert (await async_client.get(ROOT + "/profiles/arbitrary-other-account-id")).status_code == 404


async def test_profile_auth_expiry_marks_connection_invalid(async_client, cloud_service):
    await connect(async_client)
    with patch(PROFILES, new_callable=AsyncMock, side_effect=AnycubicCloudAuthError()):
        response = await async_client.get(ROOT + "/profiles")
    assert response.status_code == 401
    state = (await async_client.get(ROOT + "/status")).json()
    assert state["connected"] is False
    assert state["needs_login"] is True


async def test_profile_network_failure_preserves_credentials(async_client, cloud_service):
    await connect(async_client)
    with patch(PROFILES, new_callable=AsyncMock, side_effect=AnycubicCloudUnavailableError()):
        response = await async_client.get(ROOT + "/profiles")
    assert response.status_code == 502
    assert (await async_client.get(ROOT + "/status")).json()["connected"] is True


async def test_invalid_profile_archive_returns_safe_error_without_invalidating_token(async_client, cloud_service):
    await connect(async_client)
    with patch(PROFILES, new_callable=AsyncMock, side_effect=AnycubicProfileError("untrusted archive details")):
        response = await async_client.get(ROOT + "/profiles")
    assert response.status_code == 502
    assert "untrusted archive details" not in response.text
    assert (await async_client.get(ROOT + "/status")).json()["connected"] is True


async def test_logout_during_connect_cannot_be_overwritten_by_late_login(async_client, cloud_service):
    started, release = asyncio.Event(), asyncio.Event()

    async def pending():
        started.set()
        await release.wait()
        return PUBLIC

    cloud_service.validate_token.side_effect = pending
    task = asyncio.create_task(connect(async_client))
    await asyncio.wait_for(started.wait(), 5)
    assert (await async_client.post(ROOT + "/logout")).status_code == 200
    release.set()
    assert (await asyncio.wait_for(task, 5)).status_code == 409
    assert (await async_client.get(ROOT + "/status")).json()["connected"] is False


async def test_logout_during_profile_fetch_rejects_stale_results(async_client, cloud_service):
    from backend.app.api.routes.anycubic_cloud import _profile_cache

    await connect(async_client)
    started, release = asyncio.Event(), asyncio.Event()

    async def pending(service):
        started.set()
        await release.wait()
        return entries()

    with patch(PROFILES, side_effect=pending):
        task = asyncio.create_task(async_client.get(ROOT + "/profiles"))
        await asyncio.wait_for(started.wait(), 5)
        assert (await async_client.post(ROOT + "/logout")).status_code == 200
        release.set()
        assert (await asyncio.wait_for(task, 5)).status_code == 409
    assert not _profile_cache


async def test_new_connection_cannot_reuse_old_profile_cache(async_client, cloud_service):
    await connect(async_client)
    with patch(PROFILES, new_callable=AsyncMock, return_value=entries()) as fetch:
        assert (await async_client.get(ROOT + "/profiles")).status_code == 200
        assert (await async_client.post(ROOT + "/logout")).status_code == 200
        cloud_service.validate_token.return_value = {"user_id": "account-2", "display_name": "Second"}
        cloud_service.account_namespace = "anycubic:global:account-2"
        assert (await connect(async_client, "second-account-token")).status_code == 200
        assert (await async_client.get(ROOT + "/profiles")).status_code == 200
        assert fetch.await_count == 2


async def enable_auth(client):
    response = await client.post(
        "/api/v1/auth/setup",
        json={
            "auth_enabled": True,
            "admin_username": "cloudadmin",
            "admin_password": "AdminPass1!",
        },
    )
    assert response.status_code == 200
    login = await client.post("/api/v1/auth/login", json={"username": "cloudadmin", "password": "AdminPass1!"})
    assert login.status_code == 200
    return {"Authorization": "Bearer " + login.json()["access_token"]}


async def actor(db, name, permissions):
    from backend.app.core.auth import create_access_token
    from backend.app.models.group import Group
    from backend.app.models.user import User

    group = Group(name="group-" + name, permissions=permissions)
    user = User(username=name, role="user", groups=[group])
    db.add(user)
    await db.commit()
    return user, {"Authorization": "Bearer " + create_access_token({"sub": name})}


async def test_accounts_are_isolated_by_bambuddy_user(async_client, db_session, cloud_service):
    await enable_auth(async_client)
    from backend.app.models.user import User

    permissions = ["anycubic_cloud:auth", "anycubic_cloud:read"]
    first, first_headers = await actor(db_session, "first", permissions)
    second, second_headers = await actor(db_session, "second", permissions)
    response = await async_client.post(ROOT + "/token", headers=first_headers, json={"token": SECRET})
    assert response.status_code == 200
    assert (await async_client.get(ROOT + "/status", headers=second_headers)).json()["connected"] is False
    cloud_service.validate_token.return_value = {"user_id": "account-2", "display_name": "Second"}
    response = await async_client.post(ROOT + "/token", headers=second_headers, json={"token": "second-token"})
    assert response.status_code == 200
    assert (await async_client.get(ROOT + "/status", headers=first_headers)).json()["user_id"] == "account-1"
    assert (await async_client.get(ROOT + "/status", headers=second_headers)).json()["user_id"] == "account-2"
    tokens = (
        (await db_session.execute(select(User.anycubic_cloud_token).where(User.id.in_([first.id, second.id]))))
        .scalars()
        .all()
    )
    assert len(tokens) == 2 and tokens[0] != tokens[1]
    assert (
        await db_session.execute(select(Settings).where(Settings.key == "anycubic_cloud_token"))
    ).scalar_one_or_none() is None


async def test_auth_only_role_can_manage_connection_but_cannot_browse(async_client, db_session, cloud_service):
    await enable_auth(async_client)
    _, headers = await actor(db_session, "manager-only", ["anycubic_cloud:auth"])
    assert (await async_client.get(ROOT + "/status", headers=headers)).status_code == 200
    response = await async_client.post(ROOT + "/token", headers=headers, json={"token": SECRET})
    assert response.status_code == 200
    with patch(PROFILES) as fetch:
        assert (await async_client.get(ROOT + "/profiles", headers=headers)).status_code == 403
    fetch.assert_not_called()


async def test_read_only_role_cannot_replace_or_disconnect_connection(async_client, db_session):
    await enable_auth(async_client)
    _, headers = await actor(db_session, "reader-only", ["anycubic_cloud:read"])
    assert (await async_client.get(ROOT + "/status", headers=headers)).status_code == 200
    with patch(SERVICE) as constructor:
        response = await async_client.post(ROOT + "/token", headers=headers, json={"token": SECRET})
        assert response.status_code == 403
        assert (await async_client.post(ROOT + "/logout", headers=headers)).status_code == 403
    constructor.assert_not_called()


async def test_api_keys_require_cloud_scope_and_use_owner_connection(async_client, cloud_service):
    admin = await enable_auth(async_client)
    response = await async_client.post(ROOT + "/token", headers=admin, json={"token": SECRET})
    assert response.status_code == 200
    for allowed in (False, True):
        created = await async_client.post(
            "/api/v1/api-keys/", headers=admin, json={"name": "key-" + str(allowed), "can_access_cloud": allowed}
        )
        assert created.status_code == 200
        headers = {"X-API-Key": created.json()["key"]}
        response = await async_client.get(ROOT + "/status", headers=headers)
        assert response.status_code == (200 if allowed else 403)
        if allowed:
            assert response.json()["user_id"] == "account-1"
            assert SECRET not in response.text

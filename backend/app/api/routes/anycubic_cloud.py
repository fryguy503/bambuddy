"""Optional Anycubic Cloud profile browsing; printer control stays LAN-only."""

import hashlib
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.cloud import _cloud_api_key_gate, cloud_caller
from backend.app.core.auth import RequireAnyPermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.schemas.anycubic_cloud import (
    AnycubicAuthStatusResponse,
    AnycubicProfileDetail,
    AnycubicProfileListResponse,
    AnycubicProfileMeta,
    AnycubicTokenConnect,
)
from backend.app.services.anycubic_cloud import AnycubicCloudAuthError, AnycubicCloudError, AnycubicCloudService
from backend.app.services.anycubic_cloud_credentials import (
    AnycubicCredentials,
    ensure_current,
    load_credentials,
    read_stored,
    store_credentials,
)
from backend.app.services.anycubic_cloud_profiles import AnycubicProfileError, load_profiles
from backend.app.services.secret_fields import mask_preset

router = APIRouter(prefix="/anycubic-cloud", tags=["anycubic-cloud"], dependencies=[Depends(_cloud_api_key_gate)])

_CACHE_SECONDS = 300
_CACHE_ACCOUNTS = 32
# No access tokens in keys. Account revision + owner prevents cross-account
# reads, stale data after token replacement, and reuse of a signed-out cache.
_profile_cache: OrderedDict[tuple[int, str], tuple[float, list[dict]]] = OrderedDict()


async def _status_caller(
    request: Request,
    user: User | None = RequireAnyPermissionIfAuthEnabled(
        Permission.ANYCUBIC_CLOUD_AUTH, Permission.ANYCUBIC_CLOUD_READ
    ),
) -> User | None:
    return user if user is not None else getattr(request.state, "api_key_owner", None)


def _owner(user: User | None) -> int:
    return user.id if user else 0


def _evict(user: User | None) -> None:
    for key in list(_profile_cache):
        if key[0] == _owner(user):
            del _profile_cache[key]


def _status(credentials: AnycubicCredentials) -> AnycubicAuthStatusResponse:
    return AnycubicAuthStatusResponse(
        connected=bool(credentials.token) and not credentials.invalid,
        region=credentials.region,
        user_id=credentials.user_id,
        display_name=credentials.display_name,
        needs_login=credentials.invalid,
    )


@router.get("/status", response_model=AnycubicAuthStatusResponse)
async def status(
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = Depends(_status_caller),
):
    """Return local connection state without calling Anycubic."""
    return _status(await load_credentials(db, current_user))


@router.post("/token", response_model=AnycubicAuthStatusResponse)
async def connect_token(
    body: AnycubicTokenConnect,
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.ANYCUBIC_CLOUD_AUTH),
):
    previous = await read_stored(db, current_user)
    token = body.token.get_secret_value().strip()
    service = AnycubicCloudService(token=token, region=body.region)
    try:
        identity = await service.validate_token()
        token = service.account_token
    except AnycubicCloudAuthError:
        raise HTTPException(
            401, "Anycubic rejected the token. Sign in again in Slicer Next or the Cloud website and reconnect."
        ) from None
    except AnycubicCloudError:
        raise HTTPException(502, "Anycubic Cloud could not validate the connection. Try again later.") from None
    finally:
        await service.close()
    await store_credentials(
        db,
        current_user,
        expected=previous,
        token=token,
        region=body.region,
        user_id=identity["user_id"],
        display_name=identity.get("display_name"),
    )
    _evict(current_user)
    return _status(await load_credentials(db, current_user))


@router.post("/logout")
async def logout(
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.ANYCUBIC_CLOUD_AUTH),
):
    await store_credentials(db, current_user, expected=await read_stored(db, current_user))
    _evict(current_user)
    return {"success": True}


async def _profiles(db: AsyncSession, user: User | None, *, refresh: bool = False) -> list[dict]:
    credentials = await load_credentials(db, user)
    if not credentials.token or credentials.invalid:
        raise HTTPException(401, "Connect your Anycubic Cloud account first.")
    cache_key = (_owner(user), hashlib.sha256((credentials.stored or "").encode()).hexdigest())
    cached = _profile_cache.get(cache_key)
    if not refresh and cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        _profile_cache.move_to_end(cache_key)
        return cached[1]
    service = AnycubicCloudService(token=credentials.token, region=credentials.region)
    try:
        entries = await load_profiles(service)
        if service.account_namespace != f"anycubic:{credentials.region}:{credentials.user_id}":
            raise AnycubicCloudAuthError()
    except AnycubicCloudAuthError:
        await store_credentials(
            db,
            user,
            expected=credentials.stored,
            token=credentials.token,
            region=credentials.region,
            user_id=credentials.user_id,
            display_name=credentials.display_name,
            invalid=True,
        )
        _evict(user)
        raise HTTPException(401, "The Anycubic account token expired or was revoked. Reconnect your account.") from None
    except (AnycubicCloudError, AnycubicProfileError):
        raise HTTPException(502, "Anycubic Cloud profiles could not be read. Try again later.") from None
    finally:
        await service.close()
    await ensure_current(db, user, credentials)
    _profile_cache[cache_key] = (time.monotonic(), entries)
    _profile_cache.move_to_end(cache_key)
    while len(_profile_cache) > _CACHE_ACCOUNTS:
        _profile_cache.popitem(last=False)
    return entries


def _meta(entry: dict) -> AnycubicProfileMeta:
    return AnycubicProfileMeta(
        setting_id=entry["setting_id"],
        name=entry["name"],
        type=entry["type"],
        version=str(entry["version"]) if entry.get("version") is not None else None,
        updated_time=str(entry["updated_time"]) if entry.get("updated_time") is not None else None,
    )


@router.get("/profiles", response_model=AnycubicProfileListResponse)
async def list_profiles(
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.ANYCUBIC_CLOUD_READ),
):
    groups: dict[str, list[AnycubicProfileMeta]] = {"filament": [], "printer": [], "process": [], "unsupported": []}
    for entry in await _profiles(db, current_user, refresh=refresh):
        kind = entry["type"] if entry["type"] in groups else "unsupported"
        groups[kind].append(_meta(entry))
    return AnycubicProfileListResponse(
        filament=groups["filament"],
        printer=groups["printer"],
        process=groups["process"],
        unsupported=groups["unsupported"],
    )


@router.get("/profiles/{profile_id}", response_model=AnycubicProfileDetail)
async def get_profile(
    profile_id: str,
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.ANYCUBIC_CLOUD_READ),
):
    for entry in await _profiles(db, current_user):
        if entry["setting_id"] == profile_id:
            return AnycubicProfileDetail(**_meta(entry).model_dump(), setting=mask_preset(entry["setting"]))
    raise HTTPException(404, "Anycubic Cloud profile not found")

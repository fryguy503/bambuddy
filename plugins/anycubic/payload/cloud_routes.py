"""Optional Anycubic Cloud profile browsing; printer control stays LAN-only."""

import hashlib
import time
from collections import OrderedDict
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import ApiKeyActor, RequestActor, RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.secret_fields import mask_preset

from .cloud import AnycubicCloudAuthError, AnycubicCloudError, AnycubicCloudService, MakerOnlineCatalog, cloud_operation
from .cloud_profiles import AnycubicProfileError, load_profiles
from .cloud_storage import (
    AnycubicCredentials,
    ensure_current,
    load_credentials,
    read_stored,
    store_credentials,
)

AnycubicRegion = Literal["global", "china"]


class AnycubicTokenConnect(BaseModel):
    token: SecretStr = Field(min_length=1, max_length=16384)
    region: AnycubicRegion = "global"

    @field_validator("token")
    @classmethod
    def _clean_token(cls, token: SecretStr) -> SecretStr:
        value = token.get_secret_value().strip()
        if not value or not value.isascii() or any(not 33 <= ord(char) <= 126 for char in value):
            raise ValueError("Paste the Anycubic user token without header names or embedded whitespace")
        return SecretStr(value)


class AnycubicCloudCapabilities(BaseModel):
    profiles: bool = True
    materials: bool = True
    model_metadata: bool = True
    model_downloads: bool = False
    cloud_writes: bool = False


class AnycubicAuthStatusResponse(BaseModel):
    connected: bool
    region: AnycubicRegion = "global"
    user_id: str | None = None
    display_name: str | None = None
    auth_methods: list[Literal["token"]] = ["token"]
    capabilities: AnycubicCloudCapabilities = Field(default_factory=AnycubicCloudCapabilities)
    token_help_url: str = "https://github.com/Nino6689/hass-anycubic#option-b--web-token"
    needs_login: bool = False


class AnycubicProfileMeta(BaseModel):
    setting_id: str
    name: str
    type: str
    version: str | None = None
    updated_time: str | None = None
    is_custom: bool = True
    source: Literal["anycubic_cloud"] = "anycubic_cloud"


class AnycubicProfileListResponse(BaseModel):
    filament: list[AnycubicProfileMeta] = []
    printer: list[AnycubicProfileMeta] = []
    process: list[AnycubicProfileMeta] = []
    unsupported: list[AnycubicProfileMeta] = []
    source: Literal["anycubic_cloud"] = "anycubic_cloud"


class AnycubicProfileDetail(AnycubicProfileMeta):
    setting: dict[str, Any]


def cloud_caller(*permissions: Permission):
    async def resolve(
        _permission: User | None = RequirePermissionIfAuthEnabled(*permissions),
        actor: User | ApiKeyActor | None = RequestActor,
    ) -> User | None:
        if isinstance(actor, ApiKeyActor):
            if actor.owner is None or not actor.api_key.can_access_cloud:
                raise HTTPException(403, "An owned API key with cloud access is required")
            return actor.owner
        return actor

    return Depends(resolve)


router = APIRouter(prefix="/printer-plugins/anycubic/cloud", tags=["anycubic-plugin-cloud"])

_CACHE_SECONDS = 300
_CACHE_ACCOUNTS = 32
# No access tokens in keys. Account revision + owner prevents cross-account
# reads, stale data after token replacement, and reuse of a signed-out cache.
_profile_cache: OrderedDict[tuple[int, str], tuple[float, list[dict]]] = OrderedDict()


async def _status_caller(current_user: User | None = cloud_caller(Permission.PRINTERS_READ)) -> User | None:
    return current_user


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
    current_user: User | None = cloud_caller(Permission.CLOUD_AUTH),
):
    previous = await read_stored(db, current_user)
    token = body.token.get_secret_value().strip()
    service = AnycubicCloudService(token=token, region=body.region)
    try:
        identity = await service.validate_token()
        token = service.account_token
        makeronline_token = service.makeronline_token
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
        makeronline_token=makeronline_token,
        region=body.region,
        user_id=identity["user_id"],
        display_name=identity.get("display_name"),
    )
    _evict(current_user)
    return _status(await load_credentials(db, current_user))


@router.post("/logout")
async def logout(
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.CLOUD_AUTH),
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
    current_user: User | None = cloud_caller(Permission.FILAMENTS_READ),
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
    current_user: User | None = cloud_caller(Permission.FILAMENTS_READ),
):
    for entry in await _profiles(db, current_user):
        if entry["setting_id"] == profile_id:
            return AnycubicProfileDetail(**_meta(entry).model_dump(), setting=mask_preset(entry["setting"]))
    raise HTTPException(404, "Anycubic Cloud profile not found")


@router.get("/account")
async def account(
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.PRINTERS_READ),
):
    credentials = await load_credentials(db, current_user)
    if not credentials.token or credentials.invalid:
        raise HTTPException(401, "Connect your Anycubic Cloud account first")
    service = AnycubicCloudService(token=credentials.token, region=credentials.region)
    try:
        identity = await cloud_operation("account", service=service)
        if identity["user_id"] != credentials.user_id:
            raise AnycubicCloudAuthError()
    except AnycubicCloudAuthError:
        raise HTTPException(401, "Reconnect your Anycubic Cloud account") from None
    except AnycubicCloudError:
        raise HTTPException(502, "Anycubic account information is unavailable") from None
    finally:
        await service.close()
    await ensure_current(db, current_user, credentials)
    return identity


@router.post("/catalog/materials")
async def material_catalog(
    filters: dict[str, Any],
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.FILAMENTS_READ),
):
    credentials = await load_credentials(db, current_user)
    if not credentials.token or credentials.invalid:
        raise HTTPException(401, "Connect your Anycubic Cloud account first")
    service = AnycubicCloudService(token=credentials.token, region=credentials.region)
    try:
        identity = await service.validate_token()
        if identity["user_id"] != credentials.user_id:
            raise AnycubicCloudAuthError()
        result = await cloud_operation("material_catalog", service=service, filters=filters)
    except AnycubicCloudAuthError:
        raise HTTPException(401, "Reconnect your Anycubic Cloud account") from None
    except AnycubicCloudError:
        raise HTTPException(502, "Anycubic material catalog is unavailable") from None
    finally:
        await service.close()
    await ensure_current(db, current_user, credentials)
    return mask_preset(result)


@router.get("/models/{model_id}")
async def model_detail(
    model_id: str,
    db: AsyncSession = Depends(get_db, scope="function"),
    current_user: User | None = cloud_caller(Permission.LIBRARY_READ_OWN),
):
    credentials = await load_credentials(db, current_user)
    if not credentials.token or credentials.invalid:
        raise HTTPException(401, "Connect your Anycubic Cloud account first")
    if credentials.region != "global":
        raise HTTPException(409, "MakerOnline model browsing supports the Global region")
    catalog = MakerOnlineCatalog(credentials.makeronline_token)
    try:
        result = await cloud_operation("model", catalog=catalog, model_id=model_id)
    except AnycubicCloudError:
        raise HTTPException(502, "MakerOnline model information is unavailable") from None
    finally:
        await catalog.close()
    await ensure_current(db, current_user, credentials)
    return result

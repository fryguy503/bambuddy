"""Encrypted, owner-scoped plugin credentials using the host's generic store.

No provider-specific columns or migrations are required. An encrypted record
also binds its owner key so copying another owner's ciphertext fails closed.
"""

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.encryption import mfa_decrypt, mfa_encrypt
from backend.app.models.settings import Settings
from backend.app.models.user import User

_KEY_PREFIX = "printer_plugin:anycubic:cloud:owner:"
_DISCONNECTED_PREFIX = "anycubic-plugin-disconnected:v1:"


@dataclass(repr=False)
class AnycubicCredentials:
    token: str | None = field(default=None, repr=False)
    makeronline_token: str | None = field(default=None, repr=False)
    region: Literal["global", "china"] = "global"
    user_id: str | None = None
    display_name: str | None = None
    invalid: bool = False
    stored: str | None = field(default=None, repr=False)


def owner_key(user: User | None) -> str:
    if user is None:
        return _KEY_PREFIX + "global"
    identifier = user.id
    if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier <= 0:
        raise HTTPException(403, "A current account owner is required")
    return _KEY_PREFIX + str(identifier)


def _validate_body(body: dict) -> None:
    if body.get("region") not in {"global", "china"}:
        raise ValueError("invalid region")
    for key in ("token", "makeronline_token"):
        value = body.get(key)
        if value is not None and (
            not isinstance(value, str)
            or not 1 <= len(value) <= 16384
            or not value.isascii()
            or any(not 33 <= ord(char) <= 126 for char in value)
        ):
            raise ValueError("invalid credential")
    if body.get("makeronline_token") is not None and body["region"] != "global":
        raise ValueError("invalid credential region")
    for key in ("user_id", "display_name"):
        if body.get(key) is not None and (not isinstance(body[key], str) or len(body[key]) > 512):
            raise ValueError("invalid identity")


async def read_stored(db: AsyncSession, user: User | None) -> str | None:
    return (await db.execute(select(Settings.value).where(Settings.key == owner_key(user)))).scalar_one_or_none()


async def load_credentials(db: AsyncSession, user: User | None) -> AnycubicCredentials:
    stored = await read_stored(db, user)
    if not stored:
        return AnycubicCredentials(stored=stored)
    try:
        if not isinstance(stored, str):
            raise ValueError
        if stored.startswith(_DISCONNECTED_PREFIX):
            if re.fullmatch(r"[0-9a-f]{32}", stored[len(_DISCONNECTED_PREFIX) :]) is None:
                raise ValueError
            return AnycubicCredentials(stored=stored)
        if not stored.startswith("fernet:"):
            raise ValueError
        body = json.loads(mfa_decrypt(stored))
        if not isinstance(body, dict) or body.get("owner") != owner_key(user):
            raise ValueError
        _validate_body(body)
        return AnycubicCredentials(
            token=body.get("token"),
            makeronline_token=body.get("makeronline_token"),
            region=body["region"],
            user_id=body.get("user_id"),
            display_name=body.get("display_name"),
            invalid=body.get("invalid") is True,
            stored=stored,
        )
    except (ValueError, TypeError, RuntimeError):
        raise HTTPException(
            503, "Stored Anycubic plugin credentials cannot be decrypted. Reconnect or disconnect."
        ) from None


async def store_credentials(
    db: AsyncSession,
    user: User | None,
    *,
    expected: str | None,
    token: str | None = None,
    makeronline_token: str | None = None,
    region: Literal["global", "china"] = "global",
    user_id: str | None = None,
    display_name: str | None = None,
    invalid: bool = False,
) -> None:
    key = owner_key(user)
    if token is None and not invalid:
        # A token-free revision permits logout even if the encryption key was
        # lost and fences an earlier token-connect request still in flight.
        stored = _DISCONNECTED_PREFIX + secrets.token_hex(16)
    else:
        body = {
            "owner": key,
            "token": token,
            "makeronline_token": makeronline_token,
            "region": region,
            "user_id": user_id,
            "display_name": display_name,
            "invalid": invalid,
            "revision": secrets.token_hex(16),
        }
        try:
            _validate_body(body)
        except ValueError:
            raise HTTPException(400, "Invalid Anycubic plugin account fields") from None
        stored = mfa_encrypt(json.dumps(body, separators=(",", ":")))
        if not stored.startswith("fernet:"):
            raise HTTPException(503, "An encryption key is required to save Anycubic account credentials")
    try:
        if expected is None:
            db.add(Settings(key=key, value=stored))
            await db.flush()
        else:
            result = await db.execute(
                update(Settings).where(Settings.key == key, Settings.value == expected).values(value=stored)
            )
            if getattr(result, "rowcount", 0) != 1:
                await db.rollback()
                raise HTTPException(409, "The Anycubic account changed during this request. Refresh and retry.")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "The Anycubic account changed during this request. Refresh and retry.") from None


async def ensure_current(db: AsyncSession, user: User | None, credentials: AnycubicCredentials) -> None:
    if await read_stored(db, user) != credentials.stored:
        raise HTTPException(409, "The Anycubic account changed during this request. Refresh and retry.")

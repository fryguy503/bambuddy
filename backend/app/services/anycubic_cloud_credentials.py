"""Atomic, owner-scoped credentials for the optional cloud profile service."""

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

SETTING_KEY = "anycubic_cloud_token"
_DISCONNECTED_PREFIX = "anycubic-disconnected:v1:"


@dataclass(repr=False)
class AnycubicCredentials:
    token: str | None = field(default=None, repr=False)
    makeronline_token: str | None = field(default=None, repr=False)
    region: Literal["global", "china"] = "global"
    user_id: str | None = None
    display_name: str | None = None
    invalid: bool = False
    stored: str | None = field(default=None, repr=False)


async def read_stored(db: AsyncSession, user: User | None) -> str | None:
    if user is not None:
        return (await db.execute(select(User.anycubic_cloud_token).where(User.id == user.id))).scalar_one_or_none()
    return (await db.execute(select(Settings.value).where(Settings.key == SETTING_KEY))).scalar_one_or_none()


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
        # This provider has no legacy plaintext credential format. The only
        # unencrypted representation is a token-free logout revision above.
        if not stored.startswith("fernet:"):
            raise ValueError
        body = json.loads(mfa_decrypt(stored))
        if not isinstance(body, dict) or body.get("region", "global") not in ("global", "china"):
            raise ValueError
        token = body.get("token")
        makeronline_token = body.get("makeronline_token")
        if makeronline_token is not None and (
            not isinstance(makeronline_token, str)
            or not 1 <= len(makeronline_token) <= 16384
            or not makeronline_token.isascii()
            or any(ord(c) < 33 or ord(c) > 126 for c in makeronline_token)
            or body.get("region", "global") != "global"
        ):
            raise ValueError
        if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 16384):
            raise ValueError
        for key in ("user_id", "display_name"):
            if body.get(key) is not None and (not isinstance(body[key], str) or len(body[key]) > 512):
                raise ValueError
        return AnycubicCredentials(
            token=token,
            makeronline_token=makeronline_token,
            region=body.get("region", "global"),
            user_id=body.get("user_id"),
            display_name=body.get("display_name"),
            invalid=body.get("invalid") is True,
            stored=stored,
        )
    except (ValueError, TypeError, RuntimeError):
        raise HTTPException(
            503, "The stored Anycubic connection cannot be decrypted. Reconnect or disconnect it."
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
    # A new revision also makes a logout of an empty account invalidate an
    # older connect request still in flight. Compare-and-swap works across
    # workers, without keeping a process-local lock for every possible user.
    if token is None and not invalid:
        # Disconnect must work even when the old encryption key is missing.
        # Retain a fresh, nonsecret revision so an in-flight login cannot
        # overwrite the logout; SQL NULL would lose that compare-and-swap fence.
        stored = _DISCONNECTED_PREFIX + secrets.token_hex(16)
    else:
        body = json.dumps(
            {
                "token": token,
                "makeronline_token": makeronline_token,
                "region": region,
                "user_id": user_id,
                "display_name": display_name,
                "invalid": invalid,
                "revision": secrets.token_hex(16),
            },
            separators=(",", ":"),
        )
        stored = mfa_encrypt(body)
        if not stored.startswith("fernet:"):
            raise HTTPException(503, "Bambuddy's encryption key must be available to save an Anycubic account token.")
    try:
        if user is not None:
            match = User.anycubic_cloud_token.is_(None) if expected is None else User.anycubic_cloud_token == expected
            result = await db.execute(update(User).where(User.id == user.id, match).values(anycubic_cloud_token=stored))
        elif expected is None:
            db.add(Settings(key=SETTING_KEY, value=stored))
            await db.flush()
            await db.commit()
            return
        else:
            result = await db.execute(
                update(Settings).where(Settings.key == SETTING_KEY, Settings.value == expected).values(value=stored)
            )
        if getattr(result, "rowcount", 0) != 1:
            await db.rollback()
            raise HTTPException(409, "The Anycubic account changed during this request. Refresh and try again.")
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "The Anycubic account changed during this request. Refresh and try again.") from None


async def ensure_current(db: AsyncSession, user: User | None, credentials: AnycubicCredentials) -> None:
    if await read_stored(db, user) != credentials.stored:
        raise HTTPException(409, "The Anycubic account changed during this request. Refresh and try again.")

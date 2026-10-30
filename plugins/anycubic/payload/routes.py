"""Pre-pairing public identity inspection; never trusts a certificate implicitly."""

import asyncio

from fastapi import APIRouter, HTTPException

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.permissions import Permission

from .client import AnycubicMQTTClient
from .protocol import AnycubicError

router = APIRouter(prefix="/printer-plugins/anycubic", tags=["anycubic"])


@router.get("/inspect")
async def inspect(ip_address: str, _=RequirePermissionIfAuthEnabled(Permission.DISCOVERY_SCAN)):
    try:
        return await asyncio.to_thread(AnycubicMQTTClient.discover, ip_address)
    except AnycubicError as exc:
        raise HTTPException(422, str(exc)) from exc

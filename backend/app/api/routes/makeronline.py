"""Shared-account status; resolve/import use the registered provider seam."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.cloud import _cloud_api_key_gate, cloud_caller
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.anycubic_cloud_credentials import load_credentials

router = APIRouter(prefix="/makeronline", tags=["makeronline"], dependencies=[Depends(_cloud_api_key_gate)])


@router.get("/status")
async def status(
    db: AsyncSession = Depends(get_db, scope="function"),
    user: User | None = cloud_caller(Permission.ANYCUBIC_CLOUD_READ),
):
    account = await load_credentials(db, user)
    return {"connected": bool(account.makeronline_token) and not account.invalid, "region": account.region}

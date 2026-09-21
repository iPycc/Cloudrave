"""Authenticated storage driver discovery."""

from fastapi import APIRouter, Depends, Request

from models.tables import User
from schemas.driver import StorageDriverRead
from web.mid.auth import get_current_user


router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("/drivers", response_model=list[StorageDriverRead])
async def list_storage_drivers(
    request: Request,
    _current_user: User = Depends(get_current_user),
) -> list[StorageDriverRead]:
    return [StorageDriverRead.model_validate(item) for item in request.app.state.dep.storage_manager.catalog()]

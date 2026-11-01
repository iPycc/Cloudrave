"""Administrator database configuration and migration routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import FileResponse

from models.tables import User
from schemas.database import (
    DatabaseConfigUpdate,
    DatabaseConnectionRead,
    DatabaseConnectionTestRequest,
    DatabaseMigrationRead,
    DatabaseMigrationStartRequest,
    DatabaseProfileRead,
    DatabaseStatusRead,
)
from security.rbac import require_admin
from web.mid.auth import get_current_user


router = APIRouter(prefix="/admin/database", tags=["admin-database"])


def _manager(request: Request):
    return request.app.state.database_manager


@router.get("/status", response_model=DatabaseStatusRead)
async def database_status(
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> DatabaseStatusRead:
    require_admin(current_user)
    return await _manager(request).status(request.app.state.dep, after_seq=after_seq)


@router.put("/config", response_model=DatabaseProfileRead)
async def update_database_config(
    request: Request,
    payload: DatabaseConfigUpdate = Body(...),
    current_user: User = Depends(get_current_user),
) -> DatabaseProfileRead:
    require_admin(current_user)
    return await _manager(request).update_config(payload)


@router.post("/test", response_model=DatabaseConnectionRead)
async def test_database_connection(
    request: Request,
    payload: DatabaseConnectionTestRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> DatabaseConnectionRead:
    require_admin(current_user)
    return await _manager(request).test_config(payload)


@router.post("/migrations", response_model=DatabaseMigrationRead, status_code=202)
async def start_database_migration(
    request: Request,
    payload: DatabaseMigrationStartRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> DatabaseMigrationRead:
    require_admin(current_user)
    return await _manager(request).start_migration(
        request.app.state.dep,
        current_user,
        data_choice=payload.data_choice,
        current_password=payload.current_password,
        confirmation=payload.confirmation,
    )


@router.get("/migrations/{migration_id}", response_model=DatabaseMigrationRead)
async def get_database_migration(
    migration_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
) -> DatabaseMigrationRead:
    require_admin(current_user)
    return _manager(request).migration(migration_id, after_seq=after_seq)


@router.get("/migrations/{migration_id}/backup")
async def download_database_backup(
    migration_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    require_admin(current_user)
    path = _manager(request).backup_path(migration_id)
    return FileResponse(path, filename=path.name, media_type="application/gzip")

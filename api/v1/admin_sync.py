"""Admin routes for cursor-based bucket mirror synchronization."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from web.mid.auth import get_current_user
from models.tables import User
from schemas.sync import MountSyncRead, MountSyncRequest
from security.rbac import require_admin
from services.admin.scan import BucketScanner
from services.admin.sync import MountSyncService


router = APIRouter(prefix="/admin/mount", tags=["admin-mount-sync"])


def _service(request: Request, session) -> MountSyncService:
    dep = request.app.state.dep
    return MountSyncService(
        session=session,
        mounts=dep.get_mount_repository(session),
        policies=dep.get_policy_repository(session),
        nodes=dep.get_node_repository(session),
        objects=dep.get_object_repository(session),
        audits=dep.get_audit_repository(session),
        scanner=BucketScanner(dep.storage_manager),
    )


@router.get("/{mount_id}/sync", response_model=MountSyncRead)
async def get_mount_sync(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MountSyncRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).status(mount_id)


@router.post("/{mount_id}/sync", response_model=MountSyncRead)
async def sync_mount(
    mount_id: int,
    request: Request,
    payload: MountSyncRequest = Body(default=MountSyncRequest()),
    current_user: User = Depends(get_current_user),
) -> MountSyncRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).run(request, current_user, mount_id, payload)

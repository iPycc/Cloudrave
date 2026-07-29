"""Admin bucket mount routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, status

from web.mid.auth import get_current_user
from models.tables import User
from schemas.mount import (
    BucketMountCreate,
    BucketMountDeletePreview,
    BucketMountDeleteResult,
    BucketMountRead,
    BucketMountUpdate,
)
from security.rbac import require_admin
from services.admin.mount import MountAdminService


router = APIRouter(prefix="/admin/mount", tags=["admin-mount"])


@router.get("", response_model=list[BucketMountRead])
async def list_mounts(request: Request, current_user: User = Depends(get_current_user)) -> list[BucketMountRead]:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = MountAdminService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.list_mounts()


@router.get("/{mount_id}", response_model=BucketMountRead)
async def get_mount(mount_id: int, request: Request, current_user: User = Depends(get_current_user)) -> BucketMountRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = MountAdminService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.get_mount(mount_id)


@router.post("", response_model=BucketMountRead, status_code=status.HTTP_201_CREATED)
async def create_mount(
    request: Request,
    payload: BucketMountCreate = Body(...),
    current_user: User = Depends(get_current_user),
) -> BucketMountRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = MountAdminService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.create_mount(request, current_user, payload)


@router.patch("/{mount_id}", response_model=BucketMountRead)
async def update_mount(
    mount_id: int,
    request: Request,
    payload: BucketMountUpdate = Body(...),
    current_user: User = Depends(get_current_user),
) -> BucketMountRead:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = MountAdminService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.update_mount(request, current_user, mount_id, payload)


@router.get("/{mount_id}/delete-preview", response_model=BucketMountDeletePreview)
async def get_delete_preview(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> BucketMountDeletePreview:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = MountAdminService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.delete_preview(mount_id)


@router.delete("/{mount_id}", response_model=BucketMountDeleteResult)
async def delete_mount(
    mount_id: int,
    request: Request,
    delete_objects: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
) -> BucketMountDeleteResult:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = MountAdminService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.delete_mount(
            request,
            current_user,
            mount_id,
            delete_objects=delete_objects,
        )


@router.post("/{mount_id}/cors/apply", response_model=dict[str, object])
async def apply_mount_cors(mount_id: int, request: Request, current_user: User = Depends(get_current_user)) -> dict[str, object]:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = MountAdminService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.configure_cors(request, current_user, mount_id)

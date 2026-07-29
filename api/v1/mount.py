"""Authenticated, owner-scoped mount routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from models.enums import UserRole
from models.tables import User
from schemas.mount import BucketMountRead
from schemas.sync import MountSyncRead, MountSyncRequest
from schemas.user_mount import (
    UserMountCreate,
    UserMountDeletePreview,
    UserMountDeleteResult,
    UserMountRead,
    UserMountUpdate,
)
from services.admin.scan import BucketScanner
from services.admin.sync import MountSyncService
from services.user.mount import UserMountService
from web.mid.auth import get_current_user


router = APIRouter(prefix="/mounts", tags=["mounts"])


def require_mount_manager(user: User) -> None:
    if user.role == UserRole.GUEST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Guest users cannot create or manage mounts",
        )


def _service(request: Request, session) -> UserMountService:
    dep = request.app.state.dep
    return UserMountService(
        session=session,
        mounts=dep.get_mount_repository(session),
        policies=dep.get_policy_repository(session),
        nodes=dep.get_node_repository(session),
        uploads=dep.get_upload_repository(session),
        audits=dep.get_audit_repository(session),
        storage_manager=dep.storage_manager,
    )


def _sync_service(request: Request, session) -> MountSyncService:
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


@router.get("", response_model=list[BucketMountRead])
async def list_mounts(request: Request, current_user: User = Depends(get_current_user)) -> list[BucketMountRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).list_mounts(current_user)


@router.post("", response_model=UserMountRead, status_code=status.HTTP_201_CREATED)
async def create_mount(
    request: Request,
    payload: UserMountCreate = Body(...),
    current_user: User = Depends(get_current_user),
) -> UserMountRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).create_mount(request, current_user, payload)


@router.get("/{mount_id}", response_model=UserMountRead)
async def get_mount(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> UserMountRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).get_mount(current_user, mount_id)


@router.patch("/{mount_id}", response_model=UserMountRead)
async def update_mount(
    mount_id: int,
    request: Request,
    payload: UserMountUpdate = Body(...),
    current_user: User = Depends(get_current_user),
) -> UserMountRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).update_mount(request, current_user, mount_id, payload)


@router.get("/{mount_id}/delete-preview", response_model=UserMountDeletePreview)
async def get_delete_preview(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> UserMountDeletePreview:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).delete_preview(current_user, mount_id)


@router.delete("/{mount_id}", response_model=UserMountDeleteResult)
async def delete_mount(
    mount_id: int,
    request: Request,
    delete_objects: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
) -> UserMountDeleteResult:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).delete_mount(
            request,
            current_user,
            mount_id,
            delete_objects=delete_objects,
        )


@router.post("/{mount_id}/cors/auto-config", response_model=dict[str, object])
async def configure_cors(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).configure_cors(request, current_user, mount_id)


@router.get("/{mount_id}/sync", response_model=MountSyncRead)
async def get_mount_sync(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MountSyncRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        await _service(request, session).require_owned_mount(current_user, mount_id)
        return await _sync_service(request, session).status(mount_id)


@router.post("/{mount_id}/sync", response_model=MountSyncRead)
async def sync_mount(
    mount_id: int,
    request: Request,
    payload: MountSyncRequest = Body(default=MountSyncRequest()),
    current_user: User = Depends(get_current_user),
) -> MountSyncRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        await _service(request, session).require_owned_mount(current_user, mount_id)
        return await _sync_service(request, session).run(request, current_user, mount_id, payload)

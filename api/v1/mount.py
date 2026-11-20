"""Authenticated, owner-scoped mount routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import HttpUrl

from models.enums import UserRole
from models.tables import User
from models.tables import MountSyncTask
from schemas.mount import BucketMountRead, MountUsageRead
from schemas.sync import MountSyncRead, MountSyncRequest
from schemas.task import MountSyncTaskRead, PathMigrationRead, PathPlanRead, PathPlanRequest, PathTemplatePreview
from schemas.user_mount import (
    OssDomainRequest,
    UploadCorsRead,
    UserMountCreate,
    UserMountDeletePreview,
    UserMountDeleteResult,
    UserMountRead,
    UserMountUpdate,
)
from services.admin.scan import BucketScanner
from services.admin.sync import MountSyncService
from services.admin.sync_task import start_sync_task, stop_sync_task
from services.admin.path_plan import control_path_plan, preview_path_template, start_path_plan
from services.admin.path_migration import control_migration, start_migration
from services.user.mount import UserMountService
from web.mid.auth import get_current_user


router = APIRouter(prefix="/mounts", tags=["mounts"])


@router.post("/{mount_id}/oss/domains", response_model=dict[str, object])
async def manage_oss_domains(
    mount_id: int,
    request: Request,
    payload: OssDomainRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).manage_oss_domains(request, current_user, mount_id, payload)


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


@router.get("/usage", response_model=list[MountUsageRead])
async def list_mount_usage(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> list[MountUsageRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        mounts = await dep.get_mount_repository(session).list_for_owner(current_user.id)
        nodes = dep.get_node_repository(session)
        uploads = dep.get_upload_repository(session)
        return [
            MountUsageRead(
                mount_id=mount.id,
                used_bytes=await nodes.mount_file_bytes(mount.id),
                reserved_bytes=await uploads.reserved_bytes_for_mount(mount.id),
                quota_bytes=mount.quota_bytes,
            )
            for mount in mounts
        ]


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


@router.get("/{mount_id}/cors/check", response_model=UploadCorsRead)
async def check_upload_cors(
    mount_id: int,
    request: Request,
    origin: HttpUrl = Query(),
    current_user: User = Depends(get_current_user),
) -> UploadCorsRead:
    require_mount_manager(current_user)
    if (origin.path not in {"", "/", None} or origin.query is not None or origin.fragment is not None or
            origin.username is not None or origin.password is not None):
        raise HTTPException(status_code=422, detail="请提供站点来源地址")
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _service(request, session).check_upload_cors(current_user, mount_id, str(origin).rstrip("/"))


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


@router.post("/{mount_id}/sync/task", response_model=MountSyncTaskRead, status_code=202)
async def start_mount_sync_task(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MountSyncTaskRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        mount = await _service(request, session).require_owned_mount(current_user, mount_id)
        task = await start_sync_task(dep, session, mount, current_user.id)
        return MountSyncTaskRead.model_validate(task)


@router.post("/{mount_id}/path-plan/preview", response_model=PathTemplatePreview)
async def preview_mount_path_template(
    mount_id: int, payload: PathPlanRequest, request: Request,
    current_user: User = Depends(get_current_user),
) -> PathTemplatePreview:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await preview_path_template(dep, session, mount_id, current_user.id, payload.template)


@router.post("/{mount_id}/path-plan", response_model=PathPlanRead, status_code=202)
async def start_mount_path_plan(
    mount_id: int, payload: PathPlanRequest, request: Request,
    current_user: User = Depends(get_current_user),
) -> PathPlanRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        task = await start_path_plan(dep, session, mount_id, current_user.id, payload.template)
        return PathPlanRead.model_validate(task)


@router.post("/{mount_id}/path-plan/{task_id}/{action}", response_model=PathPlanRead)
async def control_mount_path_plan(
    mount_id: int, task_id: str, action: str, request: Request,
    current_user: User = Depends(get_current_user),
) -> PathPlanRead:
    require_mount_manager(current_user)
    if action not in {"pause", "resume", "cancel"}:
        raise HTTPException(status_code=404, detail="Unknown task action")
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        task = await control_path_plan(dep, session, mount_id, task_id, current_user.id, action)
        return PathPlanRead.model_validate(task)


@router.post("/{mount_id}/path-plan/{task_id}/migrate/start", response_model=PathMigrationRead, status_code=202)
async def start_mount_path_migration(
    mount_id: int, task_id: str, request: Request, current_user: User = Depends(get_current_user),
) -> PathMigrationRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        job = await start_migration(dep, session, mount_id, task_id, current_user.id)
        return PathMigrationRead.model_validate(job)


@router.post("/{mount_id}/path-plan/{task_id}/migrate/{action}", response_model=PathMigrationRead)
async def control_mount_path_migration(
    mount_id: int, task_id: str, action: str, request: Request, current_user: User = Depends(get_current_user),
) -> PathMigrationRead:
    require_mount_manager(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        job = await control_migration(dep, session, mount_id, task_id, current_user.id, action)
        return PathMigrationRead.model_validate(job)


@router.post("/{mount_id}/sync/task/{task_id}/{action}", response_model=MountSyncTaskRead)
async def control_mount_sync_task(
    mount_id: int,
    task_id: str,
    action: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MountSyncTaskRead:
    require_mount_manager(current_user)
    if action not in {"pause", "cancel", "resume"}:
        raise HTTPException(status_code=404, detail="Unknown task action")
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        mount = await _service(request, session).require_owned_mount(current_user, mount_id)
        if action == "resume":
            task = await start_sync_task(dep, session, mount, current_user.id, resume_task_id=task_id)
        else:
            task = await session.get(MountSyncTask, task_id)
            if task is None or task.mount_id != mount.id or task.owner_id != current_user.id:
                raise HTTPException(status_code=404, detail="Synchronization task not found")
            task = await stop_sync_task(dep, session, task, current_user.id, cancel=action == "cancel")
        return MountSyncTaskRead.model_validate(task)

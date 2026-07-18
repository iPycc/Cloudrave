"""Shared-with-me routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, status

from app.middleware.auth_context import get_current_user
from app.models.tables import User
from app.schemas.common import MessageResponse
from app.schemas.node import NodeRead
from app.schemas.shared import SharedCreate, SharedNode, SharedRead, SharedSaveRequest
from app.services.explorer.share import ShareService
from app.services.explorer.archive import ArchiveService
from app.services.explorer.shared import SharedService
from app.services.explorer.shared_io import SharedIo
from app.services.explorer.source import FileSourceService


router = APIRouter(prefix="/shared", tags=["shared"])


def _service(request: Request, session) -> SharedService:
    dep = request.app.state.dep
    source = ShareService(
        session=session,
        shares=dep.get_share_repository(session),
        nodes=dep.get_node_repository(session),
        audits=dep.get_audit_repository(session),
        share_signing_key=dep.share_signing_key,
    )
    return SharedService(shared=dep.get_shared_repository(session), source=source)


def _io(request: Request, session) -> SharedIo:
    dep = request.app.state.dep
    return SharedIo(
        shared=_service(request, session),
        source=FileSourceService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            storage_manager=dep.storage_manager,
        ),
        archive=ArchiveService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            storage_manager=dep.storage_manager,
        ),
    )


@router.post("", response_model=SharedRead, status_code=status.HTTP_201_CREATED)
async def mount_share(
    request: Request,
    payload: SharedCreate = Body(...),
    current_user: User = Depends(get_current_user),
) -> SharedRead:
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).create(current_user, payload)


@router.get("", response_model=list[SharedRead])
async def list_shared(request: Request, current_user: User = Depends(get_current_user)) -> list[SharedRead]:
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).list(current_user)


@router.get("/{mount_id}", response_model=SharedRead)
async def get_shared(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> SharedRead:
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).get(current_user, mount_id)


@router.get("/{mount_id}/nodes", response_model=list[SharedNode])
async def list_shared_nodes(
    mount_id: int,
    request: Request,
    parent_id: int | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> list[SharedNode]:
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).nodes(current_user, mount_id, parent_id)


@router.get("/{mount_id}/preview")
@router.head("/{mount_id}/preview", include_in_schema=False)
async def preview_shared_mount(
    mount_id: int,
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    async with request.app.state.dep.session_scope() as session:
        return await _io(request, session).preview(request, current_user, mount_id, node_id)


@router.get("/{mount_id}/download")
async def download_shared_mount(
    mount_id: int,
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    async with request.app.state.dep.session_scope() as session:
        return await _io(request, session).download(current_user, mount_id, node_id)


@router.post("/{mount_id}/save", response_model=NodeRead)
async def save_shared_mount(
    mount_id: int,
    request: Request,
    payload: SharedSaveRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> NodeRead:
    async with request.app.state.dep.session_scope() as session:
        node = await _io(request, session).save(
            current_user,
            mount_id,
            payload.node_id,
            payload.target_mount_id,
            payload.target_parent_id,
        )
        await request.app.state.dep.get_audit_repository(session).create(
            actor_id=current_user.id,
            action="shared.save",
            target=f"node:{node.id}",
            detail=node.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return NodeRead.model_validate(node)


@router.delete("/{mount_id}", response_model=MessageResponse)
async def unmount_share(
    mount_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    async with request.app.state.dep.session_scope() as session:
        await _service(request, session).delete(current_user, mount_id)
    return MessageResponse(message="Share unmounted")

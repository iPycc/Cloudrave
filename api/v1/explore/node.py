"""Mount and node routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import Response

from web.mid.auth import get_current_user
from models.tables import User
from schemas.common import HealthResponse
from schemas.mount import BucketMountRead
from schemas.node import (
    FileCreateRequest,
    FolderCreateRequest,
    NodeActivityRead,
    NodeCategory,
    NodeCopyRequest,
    NodeDeleteRequest,
    NodeMoveRequest,
    NodePageMetadataRead,
    NodePageRead,
    NodeRead,
    NodeRenameRequest,
    NodeSearchResult,
    NodeSort,
)
from services.user.storage import UserStorageProvisionService
from api.v1.explore.dep import node_service


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def explorer_health() -> HealthResponse:
    return HealthResponse(status="ok", service="explorer", timestamp=datetime.now(UTC))


@router.get("/mount", response_model=list[BucketMountRead])
async def list_user_mounts(request: Request, current_user: User = Depends(get_current_user)) -> list[BucketMountRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        await UserStorageProvisionService(
            policies=dep.get_policy_repository(session),
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            storage_manager=dep.storage_manager,
        ).ensure_default_local_storage(current_user)

        service = node_service(dep, session)
        return await service.list_mounts(current_user)


@router.get("/node", response_model=list[NodeRead])
async def list_nodes(
    request: Request,
    mount_id: int,
    parent_id: int | None = None,
    current_user: User = Depends(get_current_user),
) -> list[NodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.list_children(current_user, mount_id, parent_id)


@router.get("/node/page", response_model=NodePageRead)
async def list_node_page(
    request: Request,
    mount_id: int,
    parent_id: int | None = None,
    limit: int = Query(200, ge=1, le=2000),
    cursor: str | None = None,
    sort: NodeSort = "name-asc",
    folders_only: bool = False,
    current_user: User = Depends(get_current_user),
) -> NodePageRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.list_children_page(
            current_user,
            mount_id,
            parent_id,
            limit=limit,
            cursor=cursor,
            sort=sort,
            folders_only=folders_only,
        )


@router.get("/node/page/metadata", response_model=NodePageMetadataRead)
async def get_node_page_metadata(
    request: Request,
    mount_id: int,
    parent_id: int | None = None,
    folders_only: bool = False,
    current_user: User = Depends(get_current_user),
) -> NodePageMetadataRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.child_metadata(
            current_user,
            mount_id,
            parent_id,
            folders_only=folders_only,
        )


@router.get("/node/category", response_model=NodePageRead)
async def list_node_category(
    request: Request,
    mount_id: int,
    category: NodeCategory,
    limit: int = Query(200, ge=1, le=2000),
    cursor: str | None = None,
    sort: NodeSort = "name-asc",
    current_user: User = Depends(get_current_user),
) -> NodePageRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.list_category_page(
            current_user,
            mount_id,
            category,
            limit=limit,
            cursor=cursor,
            sort=sort,
        )


@router.get("/node/category/metadata", response_model=NodePageMetadataRead)
async def get_node_category_metadata(
    request: Request,
    mount_id: int,
    category: NodeCategory,
    current_user: User = Depends(get_current_user),
) -> NodePageMetadataRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.category_metadata(current_user, mount_id, category)


@router.get("/node/search", response_model=list[NodeSearchResult])
async def search_nodes(
    request: Request,
    mount_id: int,
    q: str = Query(min_length=1, max_length=255),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
) -> list[NodeSearchResult]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.search_nodes(current_user, mount_id, q, limit=limit)


@router.post("/node/folder", response_model=NodeRead)
async def create_folder(
    request: Request,
    payload: FolderCreateRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> NodeRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.create_folder(request, current_user, payload)


@router.post("/node/file", response_model=NodeRead)
async def create_file(
    request: Request,
    payload: FileCreateRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> NodeRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.create_file(request, current_user, payload)


@router.patch("/node/{node_id}", response_model=NodeRead)
async def rename_node(
    node_id: int,
    request: Request,
    payload: NodeRenameRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> NodeRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.rename_node(request, current_user, node_id, payload)


@router.delete("/node", response_model=dict[str, object])
async def delete_nodes(
    request: Request,
    payload: NodeDeleteRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.delete_nodes(request, current_user, payload)


@router.post("/node/move", response_model=list[NodeRead])
async def move_nodes(
    request: Request,
    payload: NodeMoveRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> list[NodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.move_nodes(request, current_user, payload)


@router.post("/node/copy", response_model=list[NodeRead])
async def copy_nodes(
    request: Request,
    payload: NodeCopyRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> list[NodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.copy_nodes(request, current_user, payload)


@router.get("/node/{node_id}/activity", response_model=list[NodeActivityRead])
async def list_node_activity(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> list[NodeActivityRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.list_activity(current_user, node_id)


@router.post("/node/{node_id}/open", status_code=204)
async def record_node_open(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        await service.record_open(request, current_user, node_id)
    return Response(status_code=204)

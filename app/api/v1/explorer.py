"""Explorer routes."""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse

from app.middleware.auth_context import get_current_user
from app.models.enums import StoragePolicyType
from app.models.tables import User
from app.schemas.common import HealthResponse, MessageResponse
from app.schemas.mount import BucketMountRead
from app.schemas.node import (
    DownloadLinkResponse,
    FolderCreateRequest,
    NodeCopyRequest,
    NodeDeleteRequest,
    NodeMoveRequest,
    NodeCategory,
    NodePageRead,
    NodeRead,
    NodeRenameRequest,
    NodeSearchResult,
    NodeSort,
)
from app.schemas.upload import (
    UploadPartRead,
    UploadSessionAbortRequest,
    UploadSessionCompleteRequest,
    UploadSessionCreate,
    UploadSessionPlan,
    UploadSessionRead,
)
from app.services.explorer.download import DownloadFileService
from app.services.explorer.file import FileExplorerService
from app.services.explorer.preview import build_local_preview_response
from app.services.explorer.upload import CreateUploadSessionService
from app.storage_adapters.local_fs import LocalFsAdapter
from app.services.user.storage import UserStorageProvisionService


router = APIRouter(prefix="/explorer", tags=["explorer"])


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

        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
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
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
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
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.list_children_page(
            current_user,
            mount_id,
            parent_id,
            limit=limit,
            cursor=cursor,
            sort=sort,
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
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.list_category_page(
            current_user,
            mount_id,
            category,
            limit=limit,
            cursor=cursor,
            sort=sort,
        )


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
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.search_nodes(current_user, mount_id, q, limit=limit)


@router.post("/node/folder", response_model=NodeRead)
async def create_folder(
    request: Request,
    payload: FolderCreateRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> NodeRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.create_folder(request, current_user, payload)


@router.patch("/node/{node_id}", response_model=NodeRead)
async def rename_node(
    node_id: int,
    request: Request,
    payload: NodeRenameRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> NodeRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.rename_node(request, current_user, node_id, payload)


@router.delete("/node", response_model=dict[str, object])
async def delete_nodes(
    request: Request,
    payload: NodeDeleteRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.delete_nodes(request, current_user, payload)


@router.post("/node/move", response_model=list[NodeRead])
async def move_nodes(
    request: Request,
    payload: NodeMoveRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> list[NodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.move_nodes(request, current_user, payload)


@router.post("/node/copy", response_model=list[NodeRead])
async def copy_nodes(
    request: Request,
    payload: NodeCopyRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> list[NodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.copy_nodes(request, current_user, payload)


@router.post("/upload", response_model=UploadSessionPlan)
async def create_upload_session(
    request: Request,
    payload: UploadSessionCreate = Body(...),
    current_user: User = Depends(get_current_user),
) -> UploadSessionPlan:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = CreateUploadSessionService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.create(request, current_user, payload)


@router.get("/upload/{session_id}", response_model=UploadSessionRead)
async def get_upload_session(
    session_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> UploadSessionRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = CreateUploadSessionService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.get(current_user, session_id)


@router.post("/upload/{session_id}/part", response_model=UploadPartRead)
async def upload_part(
    session_id: str,
    request: Request,
    part_number: int = Form(...),
    size: int = Form(0),
    etag: str | None = Form(None),
    file: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
) -> UploadPartRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = CreateUploadSessionService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.record_part(current_user, session_id, part_number, file, etag, size)


@router.post("/upload/{session_id}/complete", response_model=UploadSessionRead)
async def complete_upload(
    session_id: str,
    request: Request,
    payload: UploadSessionCompleteRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> UploadSessionRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = CreateUploadSessionService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.complete(request, current_user, session_id, payload)


@router.post("/upload/{session_id}/abort", response_model=UploadSessionRead)
async def abort_upload(
    session_id: str,
    request: Request,
    payload: UploadSessionAbortRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> UploadSessionRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = CreateUploadSessionService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            uploads=dep.get_upload_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.abort(request, current_user, session_id, payload)


@router.get("/download/{node_id}")
async def download_file(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = DownloadFileService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            storage_manager=dep.storage_manager,
        )
        node, mount, policy = await service.resolve(current_user, node_id)
        return service.build_file_stream(node, mount, policy)


@router.get("/download/folder/{node_id}")
async def download_folder(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = FileExplorerService(
            session=session,
            mounts=dep.get_mount_repository(session),
            policies=dep.get_policy_repository(session),
            nodes=dep.get_node_repository(session),
            objects=dep.get_object_repository(session),
            audits=dep.get_audit_repository(session),
            storage_manager=dep.storage_manager,
        )
        return await service.download_folder(request, current_user, node_id)


@router.get("/preview/{node_id}")
async def preview_file(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep

    async with dep.session_scope() as session:
        service = DownloadFileService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            storage_manager=dep.storage_manager,
        )
        node, mount, policy = await service.resolve(current_user, node_id)
        adapter = dep.storage_manager.get_adapter(policy)
        content_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
        if policy.provider == StoragePolicyType.LOCAL_FS:
            if not isinstance(adapter, LocalFsAdapter):
                raise RuntimeError("Local filesystem policy has no local adapter")
            return build_local_preview_response(
                request,
                adapter.resolve_object_path(mount, node.blob_path),
                filename=node.name,
                media_type=content_type,
            )
        signed = adapter.build_download_url(policy, mount, node.blob_path)
        return RedirectResponse(
            signed.url,
            headers={"Cache-Control": "private, max-age=30"},
        )

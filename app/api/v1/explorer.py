"""Explorer routes."""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.middleware.auth_context import get_current_user
from app.models.enums import StoragePolicyType
from app.models.tables import User
from app.schemas.common import HealthResponse, MessageResponse
from app.schemas.mount import BucketMountRead
from app.schemas.node import DownloadLinkResponse, FolderCreateRequest, NodeDeleteRequest, NodeRead, NodeRenameRequest
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
from app.services.explorer.upload import CreateUploadSessionService
from app.services.user.storage import UserStorageProvisionService


router = APIRouter(prefix="/explorer", tags=["explorer"])


def _stream_local_object(adapter, policy, mount, blob_path: str, *, media_type: str, headers: dict[str, str] | None = None):
    stream = adapter.open_object(policy, mount, blob_path)
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(stream.close),
    )


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
        )
        node, mount, policy = await service.resolve(current_user, node_id)
        adapter = dep.storage_manager.get_adapter(policy)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            content_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
            return _stream_local_object(
                adapter,
                policy,
                mount,
                node.blob_path,
                media_type=content_type,
            )
        signed = adapter.build_download_url(policy, mount, node.blob_path)
        return RedirectResponse(signed.url)


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
        )
        node, mount, policy = await service.resolve(current_user, node_id)
        adapter = dep.storage_manager.get_adapter(policy)
        content_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
        if policy.provider == StoragePolicyType.LOCAL_FS:
            return _stream_local_object(
                adapter,
                policy,
                mount,
                node.blob_path,
                media_type=content_type,
                headers={"Cache-Control": "private, max-age=3600"},
            )
        signed = adapter.build_download_url(policy, mount, node.blob_path)
        return RedirectResponse(signed.url)

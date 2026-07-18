"""Explorer routes."""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from app.middleware.auth_context import get_current_user
from app.models.enums import StoragePolicyType
from app.models.tables import User
from app.schemas.common import HealthResponse, MessageResponse
from app.schemas.mount import BucketMountRead
from app.schemas.node import (
    DownloadLinkResponse,
    FileCreateRequest,
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
from app.schemas.preview import PreviewManifestRead, PreviewPrepareRead, TextContentUpdate, TextContentUpdateRead
from app.services.explorer.download import DownloadFileService
from app.services.explorer.file import FileExplorerService
from app.services.explorer.preview import DERIVED_CACHE_CONTROL, PreviewService, build_local_preview_response
from app.services.explorer.upload import CreateUploadSessionService
from app.storage_adapters.local_fs import LocalFsAdapter
from app.services.user.storage import UserStorageProvisionService


router = APIRouter(prefix="/explorer", tags=["explorer"])


def _build_preview_service(dep, session) -> PreviewService:
    resolver = DownloadFileService(
        mounts=dep.get_mount_repository(session),
        nodes=dep.get_node_repository(session),
        policies=dep.get_policy_repository(session),
        objects=dep.get_object_repository(session),
        storage_manager=dep.storage_manager,
    )
    return PreviewService(
        resolver=resolver,
        nodes=dep.get_node_repository(session),
        objects=dep.get_object_repository(session),
        audits=dep.get_audit_repository(session),
        storage_manager=dep.storage_manager,
        token_service=dep.token_service,
        cache_root=dep.config.preview_cache_root_path,
        public_base_url=dep.config.app.public_base_url,
        office_ttl_seconds=dep.config.app.office_source_ttl_seconds,
        text_max_bytes=dep.config.app.text_preview_max_bytes,
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


@router.post("/node/file", response_model=NodeRead)
async def create_file(
    request: Request,
    payload: FileCreateRequest = Body(...),
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


@router.get("/download/archive")
async def download_archive(
    request: Request,
    node_ids: list[int] = Query(...),
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
        return await service.download_nodes(request, current_user, node_ids, filename="Cloudrave-download")


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


@router.get("/preview/{node_id}/manifest", response_model=PreviewManifestRead)
async def preview_manifest(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> PreviewManifestRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _build_preview_service(dep, session).manifest(request, current_user, node_id)


@router.get("/preview/{node_id}/image/{variant}", name="preview_image_asset")
async def preview_image_asset(
    node_id: int,
    variant: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path, filename = await _build_preview_service(dep, session).image_asset(current_user, node_id, variant)
        return build_local_preview_response(
            request,
            path,
            filename=filename,
            media_type="image/webp",
            cache_control=DERIVED_CACHE_CONTROL,
        )


@router.get("/preview/{node_id}/dzi.dzi", name="preview_dzi")
async def preview_dzi(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        descriptor = await _build_preview_service(dep, session).dzi_descriptor(current_user, node_id)
        return Response(
            descriptor,
            media_type="application/xml",
            headers={"Cache-Control": DERIVED_CACHE_CONTROL, "X-Content-Type-Options": "nosniff"},
        )


@router.get("/preview/{node_id}/dzi_files/{level}/{tile_name}")
async def preview_dzi_tile(
    node_id: int,
    level: int,
    tile_name: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path = await _build_preview_service(dep, session).dzi_tile(current_user, node_id, level, tile_name)
        return build_local_preview_response(
            request,
            path,
            filename=tile_name,
            media_type="image/webp",
            cache_control=DERIVED_CACHE_CONTROL,
        )


@router.post("/preview/{node_id}/prepare", response_model=PreviewPrepareRead)
async def prepare_preview(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> PreviewPrepareRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _build_preview_service(dep, session).prepare_media(current_user, node_id)


@router.get("/preview/{node_id}/video/poster", name="preview_video_poster")
async def preview_video_poster(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path = await _build_preview_service(dep, session).video_poster(current_user, node_id)
        return build_local_preview_response(
            request,
            path,
            filename="poster.webp",
            media_type="image/webp",
            cache_control=DERIVED_CACHE_CONTROL,
        )


@router.get("/preview/{node_id}/audio/cover", name="preview_audio_cover")
async def preview_audio_cover(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path = await _build_preview_service(dep, session).audio_cover(current_user, node_id)
        if path is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio cover not available")
        return build_local_preview_response(
            request,
            path,
            filename="cover.webp",
            media_type="image/webp",
            cache_control=DERIVED_CACHE_CONTROL,
        )


@router.get("/preview/{node_id}/media/{version}/{asset_path:path}", name="preview_media_asset")
async def preview_media_asset(
    node_id: int,
    version: str,
    asset_path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path = await _build_preview_service(dep, session).media_asset(current_user, node_id, version, asset_path)
        media_types = {
            ".m3u8": "application/vnd.apple.mpegurl",
            ".ts": "video/mp2t",
            ".mp3": "audio/mpeg",
            ".webp": "image/webp",
        }
        return build_local_preview_response(
            request,
            path,
            filename=Path(asset_path).name,
            media_type=media_types.get(path.suffix.lower(), "application/octet-stream"),
            cache_control=DERIVED_CACHE_CONTROL,
        )


@router.put("/preview/{node_id}/content", response_model=TextContentUpdateRead)
async def update_preview_text(
    node_id: int,
    payload: TextContentUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    current_user: User = Depends(get_current_user),
) -> TextContentUpdateRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _build_preview_service(dep, session).save_text(
            current_user,
            node_id,
            content=payload.content,
            if_match=if_match,
        )


@router.get("/office/source/{node_id}", name="office_source")
@router.head("/office/source/{node_id}", name="office_source_head", include_in_schema=False)
async def office_source(node_id: int, token: str, request: Request):
    """Public, short-lived source URL consumed by Microsoft Office Web Viewer."""

    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_preview_service(dep, session)
        node, mount, policy = await service.resolver.resolve_shared(node_id)
        service.validate_office_token(token, node)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            adapter = dep.storage_manager.get_adapter(policy)
            if not isinstance(adapter, LocalFsAdapter):
                raise RuntimeError("Local filesystem policy has no local adapter")
            media_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
            return build_local_preview_response(
                request,
                adapter.resolve_object_path(mount, node.blob_path),
                filename=node.name,
                media_type=media_type,
                cache_control="private, no-store",
            )
        adapter = dep.storage_manager.get_adapter(policy)
        signed = adapter.build_download_url(policy, mount, node.blob_path, expires_in=dep.config.app.office_source_ttl_seconds)
        return RedirectResponse(signed.url, headers={"Cache-Control": "private, no-store"})

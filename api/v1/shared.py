"""Shared-with-me routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from web.mid.auth import get_current_user
from models.tables import User
from schemas.common import MessageResponse
from schemas.node import NodeRead
from schemas.preview import PreviewManifestRead
from schemas.shared import SharedCreate, SharedNode, SharedRead, SharedSaveRequest, SharedSummaryRead
from services.explorer.download import DownloadFileService
from services.explorer.preview import PreviewService, relative_preview_url
from services.explorer.share import ShareService
from services.explorer.archive import ArchiveService
from services.explorer.shared import SharedService
from services.explorer.shared_io import SharedIo
from services.explorer.source import FileSourceService, content_disposition


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


def _preview(request: Request, session) -> PreviewService:
    dep = request.app.state.dep
    return PreviewService(
        resolver=DownloadFileService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            storage_manager=dep.storage_manager,
        ),
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


def _mounted_preview_url(
    request: Request,
    route_name: str,
    mount_id: int,
    node_id: int,
) -> str:
    return relative_preview_url(
        request.url_for(route_name, mount_id=mount_id).include_query_params(node_id=node_id)
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


@router.get("/summary", response_model=SharedSummaryRead)
async def shared_summary(request: Request, current_user: User = Depends(get_current_user)) -> SharedSummaryRead:
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).summary(current_user)


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


@router.get("/{mount_id}/preview/manifest", response_model=PreviewManifestRead)
async def preview_shared_mount_manifest(
    mount_id: int,
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> PreviewManifestRead:
    async with request.app.state.dep.session_scope() as session:
        _share, node = await _service(request, session).resolve_node(
            current_user,
            mount_id,
            node_id,
        )
        if node.type.value == "folder":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folders cannot be previewed",
            )
        return await _preview(request, session).shared_manifest(
            request,
            node.id,
            source_url=_mounted_preview_url(
                request,
                "preview_shared_mount",
                mount_id,
                node.id,
            ),
            archive_entry_url=_mounted_preview_url(
                request,
                "preview_shared_mount_archive_entry",
                mount_id,
                node.id,
            ),
        )


@router.get("/{mount_id}/preview/archive/entry")
async def preview_shared_mount_archive_entry(
    mount_id: int,
    node_id: int,
    path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    async with request.app.state.dep.session_scope() as session:
        _share, node = await _service(request, session).resolve_node(
            current_user,
            mount_id,
            node_id,
        )
        if node.type.value == "folder":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folders cannot be previewed",
            )
        payload, filename, media_type = await _preview(
            request,
            session,
        ).shared_archive_entry(node.id, path)
    return Response(
        payload,
        media_type=media_type,
        headers={
            "Content-Disposition": content_disposition(filename, inline=True),
            "Cache-Control": "private, no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            "X-Content-Type-Options": "nosniff",
        },
    )


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

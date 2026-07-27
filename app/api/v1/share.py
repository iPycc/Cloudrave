"""Share link routes."""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from app.middleware.auth_context import get_current_user
from app.models.enums import StoragePolicyType
from app.models.tables import Node, ShareRecord, User
from app.schemas.common import MessageResponse
from app.schemas.preview import PreviewManifestRead
from app.schemas.share import (
    ShareAccessTokenResponse,
    ShareCreateRequest,
    ShareNodeInfo,
    ShareRead,
    ShareVerifyRequest,
    SharedNodeRead,
)
from app.services.explorer.download import ArchiveDownloadService, DownloadFileService
from app.services.explorer.preview import (
    DERIVED_CACHE_CONTROL,
    PreviewService,
    build_local_preview_response,
    relative_preview_url,
)
from app.services.explorer.proxy import remote_response
from app.services.explorer.share import ShareService
from app.services.explorer.source import content_disposition
from app.services.user.response import build_avatar_url
from app.storage_adapters.local_fs import LocalFsAdapter


router = APIRouter(prefix="/share", tags=["share"])


def _build_share_service(request: Request, session):
    dep = request.app.state.dep
    return ShareService(
        session=session,
        shares=dep.get_share_repository(session),
        nodes=dep.get_node_repository(session),
        audits=dep.get_audit_repository(session),
        share_signing_key=dep.share_signing_key,
    )


def _build_preview_service(request: Request, session) -> PreviewService:
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


def _share_preview_url(
    request: Request,
    route_name: str,
    share_id: str,
    node_id: int,
    access_token: str | None,
) -> str:
    query: dict[str, str | int] = {"node_id": node_id}
    if access_token:
        query["access_token"] = access_token
    return relative_preview_url(
        request.url_for(route_name, share_id=share_id).include_query_params(**query)
    )


def _require_share_access(service: ShareService, share: ShareRecord, access_token: str | None) -> None:
    if share.access.value == "password" and (
        access_token is None or not service.verify_access_token(share, access_token)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Share access token required")


def _require_share_download_available(share: ShareRecord) -> None:
    if share.max_downloads is not None and share.download_count >= share.max_downloads:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Share download limit reached")


def _shared_node_read(node: Node) -> SharedNodeRead:
    return SharedNodeRead(
        id=node.id,
        parent_id=node.parent_id,
        name=node.name,
        type=node.type.value,
        size=node.size,
    )


@router.post("", response_model=ShareRead)
async def create_share(
    request: Request,
    payload: ShareCreateRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> ShareRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _build_share_service(request, session).create_share(request, current_user, payload)


@router.get("/my", response_model=list[ShareRead])
async def list_my_shares(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> list[ShareRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await _build_share_service(request, session).list_shares(current_user)


@router.delete("/{share_id}", response_model=MessageResponse)
async def revoke_share(
    share_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        await _build_share_service(request, session).revoke_share(request, current_user, share_id)
        return MessageResponse(message="Share revoked")


@router.get("/{share_id}", response_model=ShareNodeInfo)
async def get_share_info(
    share_id: str,
    request: Request,
    access_token: str | None = Query(None),
) -> ShareNodeInfo:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, nodes = await service.get_share_info(share_id)
        owner = await session.get(User, share.owner_id)
        may_reveal = share.access.value != "password" or (
            access_token is not None and service.verify_access_token(share, access_token)
        )
        visible_nodes = nodes if may_reveal else []
        primary = visible_nodes[0] if visible_nodes else None
        return ShareNodeInfo(
            share_id=share.id,
            access=share.access,
            node_id=primary.id if primary else None,
            node_name=primary.name if primary else "",
            node_type=primary.type.value if primary else "",
            node_size=primary.size if primary else 0,
            owner_name=owner.username if owner else "Cloudrave 用户",
            owner_uid=owner.uid if owner else "",
            owner_avatar=build_avatar_url(owner) if owner else None,
            folder_count=sum(node.type.value == "folder" for node in nodes),
            file_count=sum(node.type.value == "file" for node in nodes),
            items=[_shared_node_read(node) for node in visible_nodes],
            expires_at=share.expires_at,
            max_downloads=share.max_downloads,
            download_count=share.download_count,
            view_count=share.view_count,
        )


@router.get("/{share_id}/nodes", response_model=list[SharedNodeRead])
async def list_shared_nodes(
    share_id: str,
    request: Request,
    parent_id: int | None = Query(None),
    access_token: str | None = Query(None),
) -> list[SharedNodeRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, _ = await service.get_share_info(share_id)
        _require_share_access(service, share, access_token)
        nodes = await service.list_shared_nodes(share, parent_id)
        return [_shared_node_read(node) for node in nodes]


@router.post("/{share_id}/verify", response_model=ShareAccessTokenResponse)
async def verify_share_password(
    share_id: str,
    request: Request,
    payload: ShareVerifyRequest = Body(default=ShareVerifyRequest()),
) -> ShareAccessTokenResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        token = await _build_share_service(request, session).verify_password(share_id, payload)
        return ShareAccessTokenResponse(access_token=token)


@router.post("/{share_id}/download/record", response_model=MessageResponse)
async def record_shared_directory_download(
    share_id: str,
    request: Request,
    access_token: str | None = Query(None),
) -> MessageResponse:
    """Count one browser-managed directory download before its file streams start."""

    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, _ = await service.get_share_info(share_id)
        _require_share_access(service, share, access_token)
        _require_share_download_available(share)
        await service.record_download(share)
        return MessageResponse(message="Share download recorded")


@router.get("/{share_id}/download")
async def download_shared_file(
    share_id: str,
    request: Request,
    access_token: str | None = Query(None),
    node_id: int | None = Query(None),
    node_ids: list[int] | None = Query(None),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, _ = await service.get_share_info(share_id)
        _require_share_access(service, share, access_token)
        _require_share_download_available(share)

        if node_ids:
            requested_ids = list(dict.fromkeys(node_ids))
        elif node_id is not None:
            requested_ids = [node_id]
        else:
            requested_ids = [node.id for node in (await service.get_share_info(share_id))[1]]
        if len(requested_ids) > 100:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Too many nodes selected")
        nodes = [await service.resolve_shared_node(share, requested_id) for requested_id in requested_ids]

        download_service = DownloadFileService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            storage_manager=dep.storage_manager,
        )
        if len(nodes) == 1 and nodes[0].type.value == "file":
            node, mount, policy = await download_service.resolve_shared(nodes[0].id)
            response = await download_service.response(node, mount, policy)
        else:
            archive_service = ArchiveDownloadService(
                mounts=dep.get_mount_repository(session),
                nodes=dep.get_node_repository(session),
                policies=dep.get_policy_repository(session),
                storage_manager=dep.storage_manager,
            )
            archive_name = nodes[0].name if len(nodes) == 1 else f"Cloudrave-share-{share.id[:8]}"
            response = await archive_service.build_archive_response(nodes, filename=archive_name)
        await service.record_download(share)
        return response


@router.get("/{share_id}/preview")
@router.head("/{share_id}/preview", include_in_schema=False)
async def preview_shared_file(
    share_id: str,
    request: Request,
    access_token: str | None = Query(None),
    node_id: int | None = Query(None),
):
    """Stream one file inside a share inline without counting a download."""

    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, _ = await service.get_share_info(share_id)
        _require_share_access(service, share, access_token)
        node = await service.resolve_shared_node(share, node_id)
        if node.type.value == "folder":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Folders cannot be previewed")

        download_service = DownloadFileService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            storage_manager=dep.storage_manager,
        )
        node, mount, policy = await download_service.resolve_shared(node.id)
        adapter = dep.storage_manager.get_adapter(policy)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            if not isinstance(adapter, LocalFsAdapter):
                raise RuntimeError("Local filesystem policy has no local adapter")
            return build_local_preview_response(
                request,
                adapter.resolve_object_path(mount, node.blob_path),
                filename=node.name,
                media_type=mimetypes.guess_type(node.name)[0] or "application/octet-stream",
                cache_control="private, no-store",
            )
        return await remote_response(
            request,
            adapter=adapter,
            policy=policy,
            mount=mount,
            object_key=node.blob_path,
            filename=node.name,
        )


@router.get("/{share_id}/preview/manifest", response_model=PreviewManifestRead)
async def preview_shared_manifest(
    share_id: str,
    request: Request,
    access_token: str | None = Query(None),
    node_id: int | None = Query(None),
) -> PreviewManifestRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, _ = await service.get_share_info(share_id)
        _require_share_access(service, share, access_token)
        node = await service.resolve_shared_node(share, node_id)
        if node.type.value == "folder":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folders cannot be previewed",
            )
        return await _build_preview_service(request, session).shared_manifest(
            request,
            node.id,
            source_url=_share_preview_url(
                request,
                "preview_shared_file",
                share_id,
                node.id,
                access_token,
            ),
            archive_entry_url=_share_preview_url(
                request,
                "preview_shared_archive_entry",
                share_id,
                node.id,
                access_token,
            ),
            cover_url=_share_preview_url(
                request,
                "shared_audio_cover",
                share_id,
                node.id,
                access_token,
            ),
        )


@router.get("/{share_id}/preview/archive/entry")
async def preview_shared_archive_entry(
    share_id: str,
    path: str,
    request: Request,
    access_token: str | None = Query(None),
    node_id: int | None = Query(None),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, _ = await service.get_share_info(share_id)
        _require_share_access(service, share, access_token)
        node = await service.resolve_shared_node(share, node_id)
        if node.type.value == "folder":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folders cannot be previewed",
            )
        payload, filename, media_type = await _build_preview_service(
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


@router.get("/{share_id}/cover")
async def shared_audio_cover(
    share_id: str,
    request: Request,
    access_token: str | None = Query(None),
    node_id: int | None = Query(None),
):
    """Return the embedded cover art for an audio file shared via a share link."""

    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, _ = await service.get_share_info(share_id)
        _require_share_access(service, share, access_token)
        node = await service.resolve_shared_node(share, node_id)
        if node.type.value == "folder":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Folders cannot have cover art")

        preview_service = _build_preview_service(request, session)
        cover_path = await preview_service.shared_audio_cover(node.id)
        if cover_path is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No embedded cover art found")
        return build_local_preview_response(
            request,
            cover_path,
            filename="cover.webp",
            media_type="image/webp",
            cache_control=DERIVED_CACHE_CONTROL,
        )

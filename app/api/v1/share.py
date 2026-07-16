"""Share link routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from app.middleware.auth_context import get_current_user
from app.models.tables import User
from app.schemas.common import MessageResponse
from app.schemas.node import DownloadLinkResponse
from app.schemas.share import (
    ShareAccessTokenResponse,
    ShareCreateRequest,
    ShareNodeInfo,
    ShareRead,
    ShareVerifyRequest,
)
from app.services.explorer.download import DownloadFileService
from app.services.explorer.share import ShareService


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


@router.post("", response_model=ShareRead)
async def create_share(
    request: Request,
    payload: ShareCreateRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> ShareRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        return await service.create_share(request, current_user, payload)


@router.get("/my", response_model=list[ShareRead])
async def list_my_shares(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> list[ShareRead]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        return await service.list_shares(current_user)


@router.delete("/{share_id}", response_model=MessageResponse)
async def revoke_share(
    share_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        await service.revoke_share(request, current_user, share_id)
        return MessageResponse(message="Share revoked")


@router.get("/{share_id}", response_model=ShareNodeInfo)
async def get_share_info(
    share_id: str,
    request: Request,
) -> ShareNodeInfo:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, node = await service.get_share_info(share_id)
        return ShareNodeInfo(
            share_id=share.id,
            access=share.access,
            node_id=node.id,
            node_name=node.name,
            node_type=node.type.value,
            expires_at=share.expires_at,
            max_downloads=share.max_downloads,
            download_count=share.download_count,
            view_count=share.view_count,
        )


@router.post("/{share_id}/verify", response_model=ShareAccessTokenResponse)
async def verify_share_password(
    share_id: str,
    request: Request,
    payload: ShareVerifyRequest = Body(default=ShareVerifyRequest()),
) -> ShareAccessTokenResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        token = await service.verify_password(share_id, payload)
        return ShareAccessTokenResponse(access_token=token)


@router.get("/{share_id}/download")
async def download_shared_file(
    share_id: str,
    request: Request,
    access_token: str | None = Query(None),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = _build_share_service(request, session)
        share, node = await service.get_share_info(share_id)

        if share.access.value == "password":
            if access_token is None or not service.verify_access_token(share, access_token):
                from fastapi import HTTPException, status

                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Share access token required")

        if node.type.value == "folder":
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Folder download via share is not supported yet")

        download_service = DownloadFileService(
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            policies=dep.get_policy_repository(session),
            objects=dep.get_object_repository(session),
            storage_manager=dep.storage_manager,
        )
        node, mount, policy = await download_service.resolve_shared(node.id)
        response = download_service.build_file_stream(node, mount, policy)
        await service.record_download(share)
        return response

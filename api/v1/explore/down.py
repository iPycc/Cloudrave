"""Download and source preview routes."""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, Query, Request

from web.mid.auth import get_current_user
from models.enums import StoragePolicyType
from models.tables import User
from services.explorer.preview import build_local_preview_response
from services.explorer.proxy import remote_response
from api.v1.explore.dep import download_service, node_service


router = APIRouter()


@router.get("/download/archive")
async def download_archive(
    request: Request,
    node_ids: list[int] = Query(...),
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.download_nodes(request, current_user, node_ids, filename="Cloudrave-download")


@router.get("/download/{node_id}")
async def download_file(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = download_service(dep, session)
        node, mount, policy = await service.resolve(current_user, node_id)
        return await service.response(node, mount, policy)


@router.get("/download/folder/{node_id}")
async def download_folder(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = node_service(dep, session)
        return await service.download_folder(request, current_user, node_id)


@router.get("/preview/{node_id}")
async def preview_file(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep

    async with dep.session_scope() as session:
        service = download_service(dep, session)
        node, mount, policy = await service.resolve(current_user, node_id)
        adapter = dep.storage_manager.get_adapter(policy)
        content_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
        if policy.provider == StoragePolicyType.LOCAL_FS:
            local_path = dep.storage_manager.local_path(policy, mount, node.blob_path)
            if local_path is None:
                raise RuntimeError("Local filesystem policy has no local adapter")
            return build_local_preview_response(
                request,
                local_path,
                filename=node.name,
                media_type=content_type,
            )
        return await remote_response(
            request,
            adapter=adapter,
            policy=policy,
            mount=mount,
            object_key=node.blob_path,
            filename=node.name,
            cache_control="private, max-age=30",
        )

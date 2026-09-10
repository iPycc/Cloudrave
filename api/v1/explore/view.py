"""Derived preview, archive task, and Office source routes."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from web.mid.auth import get_current_user
from models.enums import StoragePolicyType
from models.tables import User
from schemas.preview import (
    OfficeCardPreviewRead,
    ArchiveEntryPageRead,
    PdfAnnotationDocument,
    PdfAnnotationUpdate,
    PreviewManifestRead,
    PreviewPrepareRead,
    TextContentUpdate,
    TextContentUpdateRead,
)
from schemas.node import NodeRead
from schemas.task import ArchiveExtractRequest, BackgroundTaskRead
from services.explorer.extract import list_tasks, start_extract
from services.explorer.preview import (
    DERIVED_CACHE_CONTROL,
    build_local_preview_response,
)
from services.explorer.proxy import remote_response
from services.explorer.source import content_disposition

from api.v1.explore.dep import preview_service


router = APIRouter()


@router.get("/preview/{node_id}/manifest", response_model=PreviewManifestRead)
async def preview_manifest(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> PreviewManifestRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await preview_service(dep, session).manifest(request, current_user, node_id)


@router.get("/preview/{node_id}/annotations", response_model=PdfAnnotationDocument)
async def get_preview_annotations(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> PdfAnnotationDocument:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await preview_service(dep, session).get_pdf_annotations(current_user, node_id)


@router.put("/preview/{node_id}/annotations", response_model=PdfAnnotationDocument)
async def update_preview_annotations(
    node_id: int,
    payload: PdfAnnotationUpdate,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
    current_user: User = Depends(get_current_user),
) -> PdfAnnotationDocument:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await preview_service(dep, session).save_pdf_annotations(
            current_user,
            node_id,
            source_version=payload.source_version,
            annotations=payload.annotations,
            if_match=if_match,
        )


@router.post("/archive/{node_id}/extract", response_model=BackgroundTaskRead, status_code=202)
async def extract_archive(
    node_id: int,
    request: Request,
    payload: ArchiveExtractRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> BackgroundTaskRead:
    return await start_extract(
        request.app.state.dep,
        current_user.id,
        node_id,
        payload.target_parent_id,
        payload.paths,
    )


@router.get("/task", response_model=list[BackgroundTaskRead])
async def list_background_tasks(
    current_user: User = Depends(get_current_user),
) -> list[BackgroundTaskRead]:
    return list_tasks(current_user.id)


@router.get("/preview/{node_id}/archive/entry")
async def preview_archive_entry(
    node_id: int,
    path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        payload, filename, media_type = await preview_service(dep, session).archive_entry(
            current_user,
            node_id,
            path,
        )
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


@router.get("/preview/{node_id}/archive/entries", response_model=ArchiveEntryPageRead)
async def preview_archive_entries(
    node_id: int,
    request: Request,
    offset: int = 0,
    limit: int = 200,
    current_user: User = Depends(get_current_user),
) -> ArchiveEntryPageRead:
    if offset < 0 or limit < 1 or limit > 500:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid archive page")
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        items, total, has_more = await preview_service(dep, session).archive_entries_page(
            current_user,
            node_id,
            offset=offset,
            limit=limit,
        )
        return ArchiveEntryPageRead(items=items, total=total, has_more=has_more)


@router.get("/preview/{node_id}/image/{variant}", name="preview_image_asset")
async def preview_image_asset(
    node_id: int,
    variant: str,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path, filename = await preview_service(dep, session).image_asset(current_user, node_id, variant)
        return build_local_preview_response(
            request,
            path,
            filename=filename,
            media_type="image/webp",
            cache_control=DERIVED_CACHE_CONTROL,
        )


@router.post("/preview/{node_id}/image/save-as", response_model=NodeRead, status_code=status.HTTP_201_CREATED)
async def save_preview_image_as(
    node_id: int,
    request: Request,
    name: str = Form(..., min_length=1, max_length=255),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> NodeRead:
    payload = await file.read(64 * 1024 * 1024 + 1)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await preview_service(dep, session).save_image_as(
            current_user,
            node_id,
            filename=name,
            payload=payload,
            mime_type=file.content_type or "application/octet-stream",
        )


@router.get("/preview/{node_id}/office/card", response_model=OfficeCardPreviewRead)
async def preview_office_card(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> OfficeCardPreviewRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await preview_service(dep, session).office_card_preview(current_user, node_id)


@router.get("/preview/{node_id}/office/cover", name="preview_office_cover")
async def preview_office_cover(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path = await preview_service(dep, session).office_card_cover(current_user, node_id)
        if path is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Office cover not available")
        return build_local_preview_response(
            request,
            path,
            filename="cover.webp",
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
        descriptor = await preview_service(dep, session).dzi_descriptor(current_user, node_id)
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
        path = await preview_service(dep, session).dzi_tile(current_user, node_id, level, tile_name)
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
        return await preview_service(dep, session).prepare_media(current_user, node_id)


@router.get("/preview/{node_id}/video/poster", name="preview_video_poster")
async def preview_video_poster(
    node_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        path = await preview_service(dep, session).video_poster(current_user, node_id)
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
        path = await preview_service(dep, session).audio_cover(current_user, node_id)
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
        path = await preview_service(dep, session).media_asset(current_user, node_id, version, asset_path)
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
        return await preview_service(dep, session).save_text(
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
        service = preview_service(dep, session)
        node, mount, policy = await service.resolver.resolve_shared(node_id)
        service.validate_office_token(token, node)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            local_path = dep.storage_manager.local_path(policy, mount, node.blob_path)
            if local_path is None:
                raise RuntimeError("Local filesystem policy has no local adapter")
            media_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
            return build_local_preview_response(
                request,
                local_path,
                filename=node.name,
                media_type=media_type,
                cache_control="private, no-store",
            )
        return await remote_response(
            request,
            adapter=dep.storage_manager.get_adapter(policy),
            policy=policy,
            mount=mount,
            object_key=node.blob_path,
            filename=node.name,
        )

"""Upload session routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile

from web.mid.auth import get_current_user
from models.tables import User
from schemas.upload import (
    UploadPartRead,
    UploadPartPlan,
    UploadSessionAbortRequest,
    UploadSessionCompleteRequest,
    UploadSessionCreate,
    UploadSessionPlan,
    UploadSessionRead,
)
from api.v1.explore.dep import upload_service


router = APIRouter()


@router.post("/upload", response_model=UploadSessionPlan)
async def create_upload_session(
    request: Request,
    payload: UploadSessionCreate = Body(...),
    current_user: User = Depends(get_current_user),
) -> UploadSessionPlan:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = upload_service(dep, session)
        return await service.create(request, current_user, payload)


@router.get("/upload/{session_id}", response_model=UploadSessionRead)
async def get_upload_session(
    session_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> UploadSessionRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = upload_service(dep, session)
        return await service.get(current_user, session_id)


@router.get("/upload/{session_id}/part/{part_number}/url", response_model=UploadPartPlan)
async def get_upload_part_url(
    session_id: str,
    part_number: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> UploadPartPlan:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = upload_service(dep, session)
        return await service.part_url(current_user, session_id, part_number)


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
        service = upload_service(dep, session)
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
        service = upload_service(dep, session)
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
        service = upload_service(dep, session)
        return await service.abort(request, current_user, session_id, payload)

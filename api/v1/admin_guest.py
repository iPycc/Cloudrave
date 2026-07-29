"""Guest account administration routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, status

from models.tables import User
from schemas.guest import GuestCreateRequest, GuestCreateResponse, GuestCredentialResponse, GuestRead, GuestUpdateRequest
from security.rbac import require_admin
from services.admin.guest import GuestAdminService
from web.mid.auth import get_current_user


router = APIRouter(prefix="/admin/guests", tags=["admin-guests"])


def _service(request: Request, session) -> GuestAdminService:
    dep = request.app.state.dep
    return GuestAdminService(
        session=session,
        users=dep.get_user_repository(session),
        sessions=dep.get_session_repository(session),
        mounts=dep.get_mount_repository(session),
        policies=dep.get_policy_repository(session),
        nodes=dep.get_node_repository(session),
        uploads=dep.get_upload_repository(session),
        password_service=dep.password_service,
    )


@router.get("", response_model=list[GuestRead])
async def list_guests(request: Request, current_user: User = Depends(get_current_user)) -> list[GuestRead]:
    require_admin(current_user)
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).list_guests()


@router.post("", response_model=GuestCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_guest(
    request: Request,
    payload: GuestCreateRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> GuestCreateResponse:
    require_admin(current_user)
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).create_guest(current_user, payload)


@router.patch("/{guest_id}", response_model=GuestRead)
async def update_guest(
    guest_id: int,
    request: Request,
    payload: GuestUpdateRequest = Body(...),
    current_user: User = Depends(get_current_user),
) -> GuestRead:
    require_admin(current_user)
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).update_guest(guest_id, payload)


@router.post("/{guest_id}/reset-password", response_model=GuestCredentialResponse)
async def reset_guest_password(
    guest_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> GuestCredentialResponse:
    require_admin(current_user)
    async with request.app.state.dep.session_scope() as session:
        return await _service(request, session).reset_password(guest_id)

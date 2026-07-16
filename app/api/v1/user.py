"""User routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import FileResponse

from app.middleware.auth_context import get_current_user
from app.models.tables import User
from app.security.auth_cookies import set_auth_cookies
from app.schemas.auth import LoginResponse, RegisterRequest, UserResponse
from app.schemas.common import MessageResponse
from app.schemas.user import LoginActivityEntry, PasswordChangeRequest, UserProfileUpdateRequest
from app.services.user.profile import UserProfileService
from app.services.user.register import UserRegisterService
from app.services.user.response import build_user_response


router = APIRouter(prefix="/user", tags=["user"])


@router.post("", response_model=LoginResponse)
async def register_user(request: Request, payload: RegisterRequest, response: Response) -> LoginResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserRegisterService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            policies=dep.get_policy_repository(session),
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            password_service=dep.password_service,
            token_service=dep.token_service,
            storage_manager=dep.storage_manager,
        )
        result = await service.register(request, payload)
        set_auth_cookies(response, request=request, config=dep.config, token_pair=result.token)
        result.token.refresh_token = None
        return result


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return build_user_response(current_user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    request: Request,
    payload: UserProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserProfileService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return await service.update_profile(request, current_user, payload)


@router.post("/me/avatar", response_model=UserResponse)
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserProfileService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return await service.upload_avatar(request, current_user, file)


@router.post("/me/password", response_model=MessageResponse)
async def change_password(
    request: Request,
    payload: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserProfileService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return await service.change_password(request, current_user, payload)


@router.get("/me/login-activity", response_model=list[LoginActivityEntry])
async def get_login_activity(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> list[LoginActivityEntry]:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserProfileService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return await service.list_login_activity(current_user)


@router.get("/avatar/{uid}/{filename}")
async def get_avatar(request: Request, uid: str, filename: str) -> FileResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserProfileService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return service.get_avatar_file(uid, filename)

"""User routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import FileResponse

from app.middleware.auth_context import get_current_user
from app.middleware.rate_limit import login_rate_limit
from app.models.tables import User
from app.schemas.passkey import (
    PasskeyListResponse,
    PasskeyOptionsResponse,
    PasskeyRead,
    PasskeyRenameRequest,
    PasskeyRegistrationOptionsRequest,
    PasskeyRegistrationVerifyRequest,
    PasskeyVerificationResponse,
)
from app.schemas.totp import (
    TwoFactorDisableRequest,
    TwoFactorSetupConfirmRequest,
    TwoFactorSetupConfirmResponse,
    TwoFactorSetupInitiateResponse,
    TwoFactorSetupPasswordRequest,
    TwoFactorSetupPasskeyRequest,
    TwoFactorStatusResponse,
)
from app.security.auth_cookies import set_auth_cookies
from app.schemas.auth import LoginResponse, RegisterRequest, UserResponse
from app.schemas.common import MessageResponse
from app.schemas.user import (
    LoginActivityEntry,
    PasswordChangeRequest,
    UserPreferencesResponse,
    UserPreferencesUpdateRequest,
    UserProfileUpdateRequest,
)
from app.repositories.site_settings import SiteSettingsRepository
from app.services.user.passkey import PasskeyService
from app.services.user.profile import UserProfileService
from app.services.user.register import UserRegisterService
from app.services.user.response import build_user_response
from app.services.user.totp import TOTPService


router = APIRouter(prefix="/user", tags=["user"])


@router.post("", response_model=LoginResponse)
async def register_user(
    request: Request,
    payload: RegisterRequest,
    response: Response,
    _rate_limit: None = Depends(login_rate_limit),
) -> LoginResponse:
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
            passkeys=dep.get_passkey_repository(session),
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
            passkeys=dep.get_passkey_repository(session),
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
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return await service.change_password(request, current_user, payload)


@router.get("/me/2fa", response_model=TwoFactorStatusResponse)
async def get_two_factor_status(current_user: User = Depends(get_current_user)) -> TwoFactorStatusResponse:
    return TwoFactorStatusResponse(enabled=bool(current_user.totp_enabled))


@router.post("/me/2fa/setup/password", response_model=TwoFactorSetupInitiateResponse)
async def setup_two_factor_password(
    request: Request,
    payload: TwoFactorSetupPasswordRequest,
    current_user: User = Depends(get_current_user),
) -> TwoFactorSetupInitiateResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = TOTPService(
            session=session,
            users=dep.get_user_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            token_service=dep.token_service,
            config=dep.config,
        )
        result = await service.setup_totp(
            request,
            current_user,
            password=payload.password,
            passkey_credential=None,
            passkey_ceremony_id=None,
            issuer=dep.config.app.name,
        )
        return TwoFactorSetupInitiateResponse(**result)


@router.post("/me/2fa/setup/passkey", response_model=TwoFactorSetupInitiateResponse)
async def setup_two_factor_passkey(
    request: Request,
    payload: TwoFactorSetupPasskeyRequest,
    current_user: User = Depends(get_current_user),
) -> TwoFactorSetupInitiateResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = TOTPService(
            session=session,
            users=dep.get_user_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            token_service=dep.token_service,
            config=dep.config,
        )
        result = await service.setup_totp(
            request,
            current_user,
            password=None,
            passkey_credential=payload.credential,
            passkey_ceremony_id=payload.ceremony_id,
            issuer=dep.config.app.name,
        )
        return TwoFactorSetupInitiateResponse(**result)


@router.post("/me/2fa/setup/confirm", response_model=TwoFactorSetupConfirmResponse)
async def confirm_two_factor_setup(
    request: Request,
    payload: TwoFactorSetupConfirmRequest,
    current_user: User = Depends(get_current_user),
) -> TwoFactorSetupConfirmResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = TOTPService(
            session=session,
            users=dep.get_user_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            token_service=dep.token_service,
            config=dep.config,
        )
        result = await service.confirm_setup(
            request,
            current_user,
            setup_token=payload.setup_token,
            secret=payload.secret,
            code=payload.code,
        )
        return TwoFactorSetupConfirmResponse(enabled=result["enabled"], backup_codes=result["backup_codes"])


@router.post("/me/2fa/disable", response_model=MessageResponse)
async def disable_two_factor(
    request: Request,
    payload: TwoFactorDisableRequest,
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = TOTPService(
            session=session,
            users=dep.get_user_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            token_service=dep.token_service,
            config=dep.config,
        )
        await service.disable_totp(
            request,
            current_user,
            code=payload.code,
            password=payload.password,
            passkey_credential=payload.credential,
            passkey_ceremony_id=payload.ceremony_id,
        )
        return MessageResponse(message="两步验证已关闭")


@router.patch("/me/preferences", response_model=UserPreferencesResponse)
async def update_preferences(
    request: Request,
    payload: UserPreferencesUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> UserPreferencesResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserProfileService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return await service.update_preferences(request, current_user, payload)


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
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return await service.list_login_activity(current_user)


@router.get("/me/passkeys", response_model=PasskeyListResponse)
async def list_passkeys(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> PasskeyListResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = PasskeyService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            policies=dep.get_policy_repository(session),
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            token_service=dep.token_service,
            storage_manager=dep.storage_manager,
            config=dep.config,
            site_settings=SiteSettingsRepository(session),
        )
        return await service.list_for_user(current_user)


@router.post("/me/passkeys/registration/options", response_model=PasskeyOptionsResponse)
async def begin_passkey_registration(
    request: Request,
    payload: PasskeyRegistrationOptionsRequest,
    current_user: User = Depends(get_current_user),
) -> PasskeyOptionsResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = PasskeyService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            policies=dep.get_policy_repository(session),
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            token_service=dep.token_service,
            storage_manager=dep.storage_manager,
            config=dep.config,
            site_settings=SiteSettingsRepository(session),
        )
        return await service.begin_registration(request, current_user, payload)


@router.post("/me/passkeys/registration/verify", response_model=PasskeyVerificationResponse)
async def verify_passkey_registration(
    request: Request,
    payload: PasskeyRegistrationVerifyRequest,
    current_user: User = Depends(get_current_user),
) -> PasskeyVerificationResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = PasskeyService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            policies=dep.get_policy_repository(session),
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            token_service=dep.token_service,
            storage_manager=dep.storage_manager,
            config=dep.config,
            site_settings=SiteSettingsRepository(session),
        )
        return await service.finish_registration(request, current_user, payload)


@router.delete("/me/passkeys/{passkey_id}", response_model=MessageResponse)
async def delete_passkey(
    request: Request,
    passkey_id: int,
    current_user: User = Depends(get_current_user),
) -> MessageResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = PasskeyService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            policies=dep.get_policy_repository(session),
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            token_service=dep.token_service,
            storage_manager=dep.storage_manager,
            config=dep.config,
            site_settings=SiteSettingsRepository(session),
        )
        await service.delete_for_user(request, current_user, passkey_id)
        return MessageResponse(message="通行密钥已删除")


@router.patch("/me/passkeys/{passkey_id}", response_model=PasskeyRead)
async def rename_passkey(
    request: Request,
    passkey_id: int,
    payload: PasskeyRenameRequest,
    current_user: User = Depends(get_current_user),
) -> PasskeyRead:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = PasskeyService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            policies=dep.get_policy_repository(session),
            mounts=dep.get_mount_repository(session),
            nodes=dep.get_node_repository(session),
            token_service=dep.token_service,
            storage_manager=dep.storage_manager,
            config=dep.config,
            site_settings=SiteSettingsRepository(session),
        )
        return await service.rename_for_user(request, current_user, passkey_id, payload)


@router.get("/avatar/{uid}/{filename}")
async def get_avatar(request: Request, uid: str, filename: str) -> FileResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserProfileService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            passkeys=dep.get_passkey_repository(session),
            password_service=dep.password_service,
            config=dep.config,
        )
        return service.get_avatar_file(uid, filename)

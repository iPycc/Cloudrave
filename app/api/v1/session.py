"""Session routes."""

from __future__ import annotations
from fastapi import APIRouter, Body, Request, Response
from fastapi import APIRouter, Request
from app.schemas.auth import LoginRequest, LoginResponse, LogoutRequest, RefreshRequest, TokenPairResponse
from app.schemas.auth import LoginRequest, LoginResponse, RefreshRequest, TokenPairResponse
from app.security.auth_cookies import clear_auth_cookies, resolve_refresh_token, set_auth_cookies
from app.schemas.common import MessageResponse
from app.services.session.refresh import RefreshTokenService
from app.services.session.revoke import RevokeSessionService


router = APIRouter(prefix="/session", tags=["session"])


@router.post("/token", response_model=LoginResponse)
async def login(request: Request, payload: LoginRequest, response: Response) -> LoginResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        service = UserLoginService(
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
        result = await service.login(request, payload)
        set_auth_cookies(response, request=request, config=dep.config, token_pair=result.token)
        result.token.refresh_token = None
        return result


@router.post("/token/refresh", response_model=TokenPairResponse)
async def refresh_token(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = Body(default=None),
) -> TokenPairResponse:
    dep = request.app.state.dep
    refresh_token_value = resolve_refresh_token(request, payload.refresh_token if payload else None)
    if not refresh_token_value:
        clear_auth_cookies(response, request=request, config=dep.config)
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token missing")
    async with dep.session_scope() as session:
        service = RefreshTokenService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            token_service=dep.token_service,
        )
        result = await service.refresh(request, RefreshRequest(refresh_token=refresh_token_value))
        set_auth_cookies(response, request=request, config=dep.config, token_pair=result)
        result.refresh_token = None
        return result


@router.delete("/token", response_model=MessageResponse)
async def revoke_token(
    request: Request,
    response: Response,
    payload: LogoutRequest | None = Body(default=None),
) -> MessageResponse:
    dep = request.app.state.dep
    logout_payload = payload or LogoutRequest()
    refresh_token_value = resolve_refresh_token(request, logout_payload.refresh_token)
    if not refresh_token_value:
        clear_auth_cookies(response, request=request, config=dep.config)
        return MessageResponse(message="Logged out locally")
    async with dep.session_scope() as session:
        service = RevokeSessionService(
            session=session,
            users=dep.get_user_repository(session),
            sessions=dep.get_session_repository(session),
            audits=dep.get_audit_repository(session),
            token_service=dep.token_service,
        )
        result = await service.revoke(
            request,
            LogoutRequest(scope=logout_payload.scope, refresh_token=refresh_token_value),
        )
        clear_auth_cookies(response, request=request, config=dep.config)
        scope_label = "all" if result["scope"] == "all" else "current"
        return MessageResponse(message=f"Revoked {result['revoked_sessions']} {scope_label} session(s)")

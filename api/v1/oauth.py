"""GitHub OAuth configuration and browser redirect routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from core.log import LogOperation, LogOutcome, get_logger
from models.enums import UserRole, UserStatus
from models.tables import User
from repositories.oauth import GitHubOAuthSettingsRepository, OAuthIdentityRepository
from schemas.oauth import (
    GitHubOAuthAdminSettings,
    GitHubOAuthConnection,
    GitHubOAuthPublicSettings,
    GitHubOAuthSettingsUpdate,
)
from security.auth_cookies import set_auth_cookies, set_oauth_two_factor_cookie
from security.rbac import require_admin
from services.oauth.github import (
    GitHubIdentityConflictError,
    GitHubIdentityNotLinkedError,
    GitHubOAuthClient,
    GitHubOAuthIdentityService,
    GitHubOAuthProtocolError,
    GitHubOAuthSettingsService,
)
from services.session.issue import IssueSessionService
from services.session.two_factor_token import TwoFactorTokenService
from services.site.settings import WebsiteSettingsService
from web.client_ip import get_client_ip
from web.mid.auth import get_current_user
from web.mid.rate import login_rate_limit


router = APIRouter(prefix="/oauth", tags=["oauth"])
LOGGER = get_logger("cloudrave.oauth.github")
GITHUB_STATE_COOKIE = "cloudrave_github_oauth_state"
GITHUB_VERIFIER_COOKIE = "cloudrave_github_oauth_verifier"
GITHUB_FLOW_COOKIE = "cloudrave_github_oauth_flow"
GITHUB_COOKIE_PATH = "/api/v1/oauth/github"
GITHUB_FLOW_TTL_SECONDS = 600


def _github_avatar_url(subject: str | None) -> str | None:
    github_user_id = str(subject or "").strip()
    if not github_user_id.isdigit():
        return None
    return f"https://avatars.githubusercontent.com/u/{github_user_id}?v=4"


async def _settings_service(request: Request, session) -> GitHubOAuthSettingsService:
    site_url = await WebsiteSettingsService(
        request.app.state.dep.get_site_settings_repository(session)
    ).get_site_url()
    return GitHubOAuthSettingsService(
        repository=GitHubOAuthSettingsRepository(session),
        config=request.app.state.dep.config,
        site_url=site_url,
    )


@router.get("/github", response_model=GitHubOAuthPublicSettings)
async def get_github_oauth_status(request: Request) -> GitHubOAuthPublicSettings:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await (await _settings_service(request, session)).get_public()


@router.get("/github/connection", response_model=GitHubOAuthConnection)
async def get_github_oauth_connection(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> GitHubOAuthConnection:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        settings = await (await _settings_service(request, session)).get_public()
        identity = await OAuthIdentityRepository(session).get_for_user(current_user.id, "github")
        return GitHubOAuthConnection(
            enabled=settings.enabled,
            linked=identity is not None,
            provider_username=identity.provider_username if identity else None,
            provider_avatar_url=_github_avatar_url(identity.subject if identity else None),
        )


@router.delete("/github/connection", response_model=GitHubOAuthConnection)
async def unlink_github_oauth_connection(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> GitHubOAuthConnection:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        settings = await (await _settings_service(request, session)).get_public()
        identity_service = GitHubOAuthIdentityService(
            identities=OAuthIdentityRepository(session),
            users=dep.get_user_repository(session),
        )
        identity = await identity_service.unlink(current_user)
        if identity is not None:
            await dep.get_audit_repository(session).create(
                actor_id=current_user.id,
                action="oauth_unlink",
                target="provider:github",
                detail=json.dumps(
                    {"provider_username": identity.provider_username},
                    ensure_ascii=False,
                ),
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
        return GitHubOAuthConnection(enabled=settings.enabled)


@router.get("/github/settings", response_model=GitHubOAuthAdminSettings)
async def get_github_oauth_settings(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> GitHubOAuthAdminSettings:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await (await _settings_service(request, session)).get_admin()


@router.put("/github/settings", response_model=GitHubOAuthAdminSettings)
async def update_github_oauth_settings(
    request: Request,
    payload: GitHubOAuthSettingsUpdate,
    current_user: User = Depends(get_current_user),
) -> GitHubOAuthAdminSettings:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        result = await (await _settings_service(request, session)).update(payload)
        await dep.get_audit_repository(session).create(
            actor_id=current_user.id,
            action="oauth_settings",
            target="provider:github",
            detail=json.dumps(
                {
                    "enabled": result.enabled,
                    "client_id": result.client_id,
                    "client_secret_configured": result.client_secret_configured,
                },
                ensure_ascii=False,
            ),
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return result


@router.get("/github/start", dependencies=[Depends(login_rate_limit)])
async def start_github_oauth(request: Request) -> RedirectResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        try:
            credentials = await (await _settings_service(request, session)).get_credentials()
        except GitHubOAuthProtocolError:
            return _login_redirect("not_configured")

    return _authorization_redirect(request, dep.config, credentials, flow="login")


@router.get("/github/link", dependencies=[Depends(login_rate_limit)])
async def start_github_oauth_link(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    dep = request.app.state.dep
    if current_user.role == UserRole.GUEST:
        return _link_redirect("guest_not_supported")

    async with dep.session_scope() as session:
        try:
            credentials = await (await _settings_service(request, session)).get_credentials()
        except GitHubOAuthProtocolError:
            return _link_redirect("not_configured")

    flow = _encode_link_flow(current_user.uid, dep.config.auth.jwt_secret)
    return _authorization_redirect(request, dep.config, credentials, flow=flow)


@router.get("/github/callback", dependencies=[Depends(login_rate_limit)])
async def finish_github_oauth(
    request: Request,
    code: str | None = Query(default=None, max_length=512),
    state_value: str | None = Query(default=None, alias="state", max_length=512),
    error: str | None = Query(default=None, max_length=128),
) -> RedirectResponse:
    dep = request.app.state.dep
    flow = request.cookies.get(GITHUB_FLOW_COOKIE, "login")
    is_link_flow = flow.startswith("link:")
    link_user_uid = _decode_link_flow(flow, dep.config.auth.jwt_secret) if is_link_flow else None
    error_redirect = _link_redirect if is_link_flow else _login_redirect
    expected_state = request.cookies.get(GITHUB_STATE_COOKIE, "")
    verifier = request.cookies.get(GITHUB_VERIFIER_COOKIE, "")
    if error or not code:
        return _clear_flow_cookies(error_redirect("access_denied"), request, dep.config)
    if not expected_state or not state_value or not hmac.compare_digest(expected_state, state_value) or not verifier:
        return _clear_flow_cookies(error_redirect("invalid_state"), request, dep.config)
    if is_link_flow and link_user_uid is None:
        return _clear_flow_cookies(_link_redirect("invalid_state"), request, dep.config)

    async with dep.session_scope() as session:
        try:
            credentials = await (await _settings_service(request, session)).get_credentials()
            profile = await GitHubOAuthClient().exchange_code(
                credentials,
                code=code,
                code_verifier=verifier,
            )
        except GitHubOAuthProtocolError:
            LOGGER.warning(
                "GitHub OAuth callback failed",
                extra={
                    "operation": LogOperation.LOGIN,
                    "resource": "oauth:github",
                    "outcome": LogOutcome.DENIED,
                    "request_id": getattr(request.state, "request_id", "-"),
                    "ip": get_client_ip(request),
                    "actor": "github",
                },
            )
            return _clear_flow_cookies(error_redirect("provider_error"), request, dep.config)

        identities = OAuthIdentityRepository(session)
        users = dep.get_user_repository(session)
        identity_service = GitHubOAuthIdentityService(identities=identities, users=users)

        if is_link_flow:
            user = await users.get_by_uid(link_user_uid)
            if user is None or user.status != UserStatus.ACTIVE:
                return _clear_flow_cookies(_link_redirect("account_not_found"), request, dep.config)
            if user.role == UserRole.GUEST:
                return _clear_flow_cookies(_link_redirect("guest_not_supported"), request, dep.config)
            try:
                await identity_service.link(user, profile)
            except GitHubIdentityConflictError:
                return _clear_flow_cookies(_link_redirect("identity_conflict"), request, dep.config)
            await dep.get_audit_repository(session).create(
                actor_id=user.id,
                action="oauth_link",
                target="provider:github",
                detail=json.dumps(
                    {
                        "provider_username": profile.login,
                        "provider_email": profile.email,
                    },
                    ensure_ascii=False,
                ),
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
            return _clear_flow_cookies(_link_redirect("success"), request, dep.config)

        try:
            user = await identity_service.resolve_login(profile)
        except GitHubIdentityNotLinkedError:
            await _record_login(
                request,
                session,
                actor_id=None,
                identifier=profile.email,
                result="failure",
            )
            return _clear_flow_cookies(_login_redirect("not_linked"), request, dep.config)

        if user.status != UserStatus.ACTIVE:
            await _record_login(request, session, actor_id=user.id, identifier=user.email, result="disabled")
            return _clear_flow_cookies(_login_redirect("account_disabled"), request, dep.config)
        if user.role == UserRole.GUEST:
            await _record_login(request, session, actor_id=user.id, identifier=user.email, result="failure")
            return _clear_flow_cookies(_login_redirect("guest_not_supported"), request, dep.config)

        if user.totp_enabled and user.totp_secret:
            two_factor_token = TwoFactorTokenService(dep.token_service).issue_login_token(
                user.uid,
                login_method="github",
            )
            response = _login_result_redirect("two_factor")
            set_oauth_two_factor_cookie(
                response,
                request=request,
                config=dep.config,
                token=two_factor_token,
            )
            return _clear_flow_cookies(response, request, dep.config)

        session_record, token_pair = await IssueSessionService(
            session=session,
            sessions=dep.get_session_repository(session),
            token_service=dep.token_service,
        ).issue_for_user(request, user)
        await _record_login(
            request,
            session,
            actor_id=user.id,
            identifier=user.email,
            result="success",
            session_id=session_record.id,
        )
        response = _login_result_redirect("success")
        set_auth_cookies(response, request=request, config=dep.config, token_pair=token_pair)
        return _clear_flow_cookies(response, request, dep.config)


async def _record_login(
    request: Request,
    session,
    *,
    actor_id: int | None,
    identifier: str,
    result: str,
    session_id: str | None = None,
) -> None:
    await request.app.state.dep.get_audit_repository(session).create(
        actor_id=actor_id,
        action="login",
        target=identifier.lower(),
        detail=json.dumps(
            {
                "method": "GitHub 登录",
                "result": result,
                "identifier": identifier,
                "device_fingerprint": request.headers.get("user-agent", "Unknown device"),
                "session_id": session_id,
            },
            ensure_ascii=False,
        ),
        ip=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


def _login_redirect(error_code: str) -> RedirectResponse:
    return _login_result_redirect(error_code)


def _login_result_redirect(result_code: str) -> RedirectResponse:
    return _oauth_complete_redirect("login", result_code)


def _link_redirect(result_code: str) -> RedirectResponse:
    return _oauth_complete_redirect("link", result_code)


def _oauth_complete_redirect(flow: str, result_code: str) -> RedirectResponse:
    return RedirectResponse(
        f"/oauth/github/complete?{urlencode({'flow': flow, 'result': result_code})}",
        status_code=status.HTTP_302_FOUND,
    )


def _authorization_redirect(request: Request, config, credentials, *, flow: str) -> RedirectResponse:
    state_value = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    target = GitHubOAuthClient.authorization_url(
        credentials,
        state=state_value,
        code_challenge=challenge,
    )
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    _set_flow_cookie(response, request, config, GITHUB_STATE_COOKIE, state_value)
    _set_flow_cookie(response, request, config, GITHUB_VERIFIER_COOKIE, verifier)
    _set_flow_cookie(response, request, config, GITHUB_FLOW_COOKIE, flow)
    return response


def _encode_link_flow(user_uid: str, secret: str, *, issued_at: int | None = None) -> str:
    timestamp = int(time.time()) if issued_at is None else issued_at
    payload = f"{user_uid}:{timestamp}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"link:{payload}:{signature}"


def _decode_link_flow(value: str, secret: str, *, now: int | None = None) -> str | None:
    try:
        prefix, user_uid, issued_at_raw, signature = value.split(":", 3)
        issued_at = int(issued_at_raw)
    except (TypeError, ValueError):
        return None
    if prefix != "link" or not user_uid:
        return None

    payload = f"{user_uid}:{issued_at}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None

    current_time = int(time.time()) if now is None else now
    if issued_at > current_time + 30 or current_time - issued_at > GITHUB_FLOW_TTL_SECONDS:
        return None
    return user_uid


def _set_flow_cookie(response: RedirectResponse, request: Request, config, name: str, value: str) -> None:
    response.set_cookie(
        key=name,
        value=value,
        httponly=True,
        secure=_secure_cookie(request, config),
        samesite="lax",
        path=GITHUB_COOKIE_PATH,
        max_age=600,
    )


def _clear_flow_cookies(response: RedirectResponse, request: Request, config) -> RedirectResponse:
    secure = _secure_cookie(request, config)
    for name in (GITHUB_STATE_COOKIE, GITHUB_VERIFIER_COOKIE, GITHUB_FLOW_COOKIE):
        response.delete_cookie(
            name,
            httponly=True,
            secure=secure,
            samesite="lax",
            path=GITHUB_COOKIE_PATH,
        )
    return response


def _secure_cookie(request: Request, config) -> bool:
    if config.app.env.strip().lower() in {"production", "prod"}:
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"

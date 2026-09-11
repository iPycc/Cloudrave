"""Google OAuth configuration and browser redirect routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse

from core.log import LogOperation, LogOutcome, get_logger
from models.enums import UserRole, UserStatus
from models.tables import User
from repositories.oauth import GoogleOAuthSettingsRepository, OAuthIdentityRepository
from schemas.oauth import (
    GoogleOAuthAdminSettings,
    GoogleOAuthConnection,
    GoogleOAuthPublicSettings,
    GoogleOAuthSettingsUpdate,
)
from security.auth_cookies import set_auth_cookies, set_oauth_two_factor_cookie
from security.rbac import require_admin
from services.oauth.google import (
    GoogleIdentityConflictError,
    GoogleIdentityNotLinkedError,
    GoogleOAuthClient,
    GoogleOAuthIdentityService,
    GoogleOAuthProtocolError,
    GoogleOAuthSettingsService,
)
from services.session.issue import IssueSessionService
from services.session.two_factor_token import TwoFactorTokenService
from services.site.settings import WebsiteSettingsService
from services.oauth.avatar_cache import OAuthAvatarCacheService
from web.client_ip import get_client_ip
from web.mid.auth import get_current_user
from web.mid.rate import login_rate_limit


router = APIRouter(prefix="/oauth/google", tags=["oauth"])
LOGGER = get_logger("cloudrave.oauth.google")
GOOGLE_STATE_COOKIE = "cloudrave_google_oauth_state"
GOOGLE_VERIFIER_COOKIE = "cloudrave_google_oauth_verifier"
GOOGLE_FLOW_COOKIE = "cloudrave_google_oauth_flow"
GOOGLE_COOKIE_PATH = "/api/v1/oauth/google"
GOOGLE_FLOW_TTL_SECONDS = 600


async def _settings_service(request: Request, session) -> GoogleOAuthSettingsService:
    site_url = await WebsiteSettingsService(
        request.app.state.dep.get_site_settings_repository(session)
    ).get_site_url()
    return GoogleOAuthSettingsService(
        repository=GoogleOAuthSettingsRepository(session),
        config=request.app.state.dep.config,
        site_url=site_url,
    )


async def _cache_google_avatar(dep, profile) -> str | None:
    cache = OAuthAvatarCacheService(config=dep.config)
    return (await cache.cache(provider="google", subject=profile.subject, external_url=profile.avatar_url)) or profile.avatar_url


async def _resolve_google_avatar(session, repository, identity, config) -> str | None:
    if identity is None:
        return None
    current = identity.provider_avatar_url
    if current and current.startswith("/api/v1/oauth/"):
        return current
    external = current if (current and current.startswith("http")) else None
    if not external:
        return None
    local = await OAuthAvatarCacheService(config=config).cache(
        provider="google", subject=identity.subject, external_url=external
    )
    if local:
        await repository.set_provider_avatar_url(identity, local)
        return local
    return external


@router.get("/avatar/{filename}", response_class=FileResponse)
async def get_google_oauth_avatar(request: Request, filename: str) -> FileResponse:
    dep = request.app.state.dep
    try:
        return OAuthAvatarCacheService(config=dep.config).get_avatar_file("google", filename)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="头像不存在")


@router.get("", response_model=GoogleOAuthPublicSettings)
async def get_google_oauth_status(request: Request) -> GoogleOAuthPublicSettings:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await (await _settings_service(request, session)).get_public()


@router.get("/connection", response_model=GoogleOAuthConnection)
async def get_google_oauth_connection(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> GoogleOAuthConnection:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        settings = await (await _settings_service(request, session)).get_public()
        repository = OAuthIdentityRepository(session)
        identity = await repository.get_for_user(current_user.id, "google")
        avatar_url = await _resolve_google_avatar(session, repository, identity, dep.config)
        return GoogleOAuthConnection(
            enabled=settings.enabled,
            linked=identity is not None,
            provider_username=identity.provider_username if identity else None,
            provider_avatar_url=avatar_url,
        )


@router.delete("/connection", response_model=GoogleOAuthConnection)
async def unlink_google_oauth_connection(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> GoogleOAuthConnection:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        settings = await (await _settings_service(request, session)).get_public()
        identity_service = GoogleOAuthIdentityService(
            identities=OAuthIdentityRepository(session),
            users=dep.get_user_repository(session),
        )
        identity = await identity_service.unlink(current_user)
        if identity is not None:
            await dep.get_audit_repository(session).create(
                actor_id=current_user.id,
                action="oauth_unlink",
                target="provider:google",
                detail=json.dumps(
                    {"provider_username": identity.provider_username},
                    ensure_ascii=False,
                ),
                ip=get_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
        return GoogleOAuthConnection(enabled=settings.enabled)


@router.get("/settings", response_model=GoogleOAuthAdminSettings)
async def get_google_oauth_settings(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> GoogleOAuthAdminSettings:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        return await (await _settings_service(request, session)).get_admin()


@router.put("/settings", response_model=GoogleOAuthAdminSettings)
async def update_google_oauth_settings(
    request: Request,
    payload: GoogleOAuthSettingsUpdate,
    current_user: User = Depends(get_current_user),
) -> GoogleOAuthAdminSettings:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        result = await (await _settings_service(request, session)).update(payload)
        await dep.get_audit_repository(session).create(
            actor_id=current_user.id,
            action="oauth_settings",
            target="provider:google",
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


@router.get("/start", dependencies=[Depends(login_rate_limit)])
async def start_google_oauth(request: Request) -> RedirectResponse:
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        try:
            credentials = await (await _settings_service(request, session)).get_credentials()
        except GoogleOAuthProtocolError:
            return _login_redirect("not_configured")

    return _authorization_redirect(request, dep.config, credentials, flow="login")


@router.get("/link", dependencies=[Depends(login_rate_limit)])
async def start_google_oauth_link(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    dep = request.app.state.dep
    if current_user.role == UserRole.GUEST:
        return _link_redirect("guest_not_supported")

    async with dep.session_scope() as session:
        try:
            credentials = await (await _settings_service(request, session)).get_credentials()
        except GoogleOAuthProtocolError:
            return _link_redirect("not_configured")

    flow = _encode_link_flow(current_user.uid, dep.config.auth.jwt_secret)
    return _authorization_redirect(request, dep.config, credentials, flow=flow)


@router.get("/callback", dependencies=[Depends(login_rate_limit)])
async def finish_google_oauth(
    request: Request,
    code: str | None = Query(default=None, max_length=2048),
    state_value: str | None = Query(default=None, alias="state", max_length=512),
    error: str | None = Query(default=None, max_length=128),
) -> RedirectResponse:
    dep = request.app.state.dep
    flow = request.cookies.get(GOOGLE_FLOW_COOKIE, "login")
    is_link_flow = flow.startswith("link:")
    link_user_uid = _decode_link_flow(flow, dep.config.auth.jwt_secret) if is_link_flow else None
    error_redirect = _link_redirect if is_link_flow else _login_redirect
    expected_state = request.cookies.get(GOOGLE_STATE_COOKIE, "")
    verifier = request.cookies.get(GOOGLE_VERIFIER_COOKIE, "")
    if error or not code:
        return _clear_flow_cookies(error_redirect("access_denied"), request, dep.config)
    if not expected_state or not state_value or not hmac.compare_digest(expected_state, state_value) or not verifier:
        return _clear_flow_cookies(error_redirect("invalid_state"), request, dep.config)
    if is_link_flow and link_user_uid is None:
        return _clear_flow_cookies(_link_redirect("invalid_state"), request, dep.config)

    async with dep.session_scope() as session:
        try:
            credentials = await (await _settings_service(request, session)).get_credentials()
            profile = await GoogleOAuthClient().exchange_code(
                credentials,
                code=code,
                code_verifier=verifier,
            )
        except GoogleOAuthProtocolError:
            LOGGER.warning(
                "Google OAuth callback failed",
                extra={
                    "operation": LogOperation.LOGIN,
                    "resource": "oauth:google",
                    "outcome": LogOutcome.DENIED,
                    "request_id": getattr(request.state, "request_id", "-"),
                    "ip": get_client_ip(request),
                    "actor": "google",
                },
            )
            return _clear_flow_cookies(error_redirect("provider_error"), request, dep.config)

        avatar_url = await _cache_google_avatar(dep, profile)
        identities = OAuthIdentityRepository(session)
        users = dep.get_user_repository(session)
        identity_service = GoogleOAuthIdentityService(identities=identities, users=users)

        if is_link_flow:
            user = await users.get_by_uid(link_user_uid)
            if user is None or user.status != UserStatus.ACTIVE:
                return _clear_flow_cookies(_link_redirect("account_not_found"), request, dep.config)
            if user.role == UserRole.GUEST:
                return _clear_flow_cookies(_link_redirect("guest_not_supported"), request, dep.config)
            try:
                await identity_service.link(user, profile, provider_avatar_url=avatar_url)
            except GoogleIdentityConflictError:
                return _clear_flow_cookies(_link_redirect("identity_conflict"), request, dep.config)
            await dep.get_audit_repository(session).create(
                actor_id=user.id,
                action="oauth_link",
                target="provider:google",
                detail=json.dumps(
                    {
                        "provider_username": profile.name,
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
            user = await identity_service.resolve_login(profile, provider_avatar_url=avatar_url)
        except GoogleIdentityNotLinkedError:
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
                login_method="google",
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
                "method": "Google 登录",
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
        f"/oauth/google/complete?{urlencode({'flow': flow, 'result': result_code})}",
        status_code=status.HTTP_302_FOUND,
    )


def _authorization_redirect(request: Request, config, credentials, *, flow: str) -> RedirectResponse:
    state_value = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    target = GoogleOAuthClient.authorization_url(
        credentials,
        state=state_value,
        code_challenge=challenge,
    )
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    _set_flow_cookie(response, request, config, GOOGLE_STATE_COOKIE, state_value)
    _set_flow_cookie(response, request, config, GOOGLE_VERIFIER_COOKIE, verifier)
    _set_flow_cookie(response, request, config, GOOGLE_FLOW_COOKIE, flow)
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
    if issued_at > current_time + 30 or current_time - issued_at > GOOGLE_FLOW_TTL_SECONDS:
        return None
    return user_uid


def _set_flow_cookie(response: RedirectResponse, request: Request, config, name: str, value: str) -> None:
    response.set_cookie(
        key=name,
        value=value,
        httponly=True,
        secure=_secure_cookie(request, config),
        samesite="lax",
        path=GOOGLE_COOKIE_PATH,
        max_age=600,
    )


def _clear_flow_cookies(response: RedirectResponse, request: Request, config) -> RedirectResponse:
    secure = _secure_cookie(request, config)
    for name in (GOOGLE_STATE_COOKIE, GOOGLE_VERIFIER_COOKIE, GOOGLE_FLOW_COOKIE):
        response.delete_cookie(
            name,
            httponly=True,
            secure=secure,
            samesite="lax",
            path=GOOGLE_COOKIE_PATH,
        )
    return response


def _secure_cookie(request: Request, config) -> bool:
    if config.app.env.strip().lower() in {"production", "prod"}:
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"

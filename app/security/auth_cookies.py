"""Helpers for issuing and clearing auth cookies."""

from __future__ import annotations

from datetime import datetime

from fastapi import Request, Response

from app.schemas.auth import TokenPairResponse
from config import CloudraveConfig


ACCESS_TOKEN_COOKIE = "cloudrave_access_token"
REFRESH_TOKEN_COOKIE = "cloudrave_refresh_token"


def set_auth_cookies(
    response: Response,
    *,
    request: Request,
    config: CloudraveConfig,
    token_pair: TokenPairResponse,
) -> None:
    secure = _should_use_secure_cookie(request, config)
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=token_pair.access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/api/v1",
        max_age=max(_seconds_until(token_pair.access_expires_at), 0),
    )

    if token_pair.refresh_token:
        response.set_cookie(
            key=REFRESH_TOKEN_COOKIE,
            value=token_pair.refresh_token,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/api/v1/session",
            max_age=max(_seconds_until(token_pair.refresh_expires_at), 0),
        )


def clear_auth_cookies(response: Response, *, request: Request, config: CloudraveConfig) -> None:
    secure = _should_use_secure_cookie(request, config)
    response.delete_cookie(ACCESS_TOKEN_COOKIE, httponly=True, secure=secure, samesite="lax", path="/api/v1")
    response.delete_cookie(REFRESH_TOKEN_COOKIE, httponly=True, secure=secure, samesite="lax", path="/api/v1/session")


def resolve_refresh_token(request: Request, payload_token: str | None) -> str | None:
    if payload_token and payload_token.strip():
        return payload_token.strip()
    cookie_token = request.cookies.get(REFRESH_TOKEN_COOKIE, "").strip()
    return cookie_token or None


def _seconds_until(value) -> int:
    now = datetime.now(value.tzinfo) if value.tzinfo else datetime.utcnow()
    return int((value - now).total_seconds())


def _should_use_secure_cookie(request: Request, config: CloudraveConfig) -> bool:
    env = config.app.env.strip().lower()
    if env in {"production", "prod"}:
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return request.url.scheme == "https" or forwarded_proto == "https"

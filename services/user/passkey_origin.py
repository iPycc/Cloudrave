"""Shared helpers for resolving WebAuthn origin / rp_id."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from core.cfg import CloudraveConfig
from services.site.settings import (
    is_local_site_url,
    is_secure_context_url,
    normalize_site_origin,
)


def resolve_passkey_origin(
    request: Request,
    config: CloudraveConfig,
    site_url: str | None = None,
) -> str:
    """Resolve and validate the WebAuthn origin for the current request.

    Localhost/127.0.0.1 origins are always allowed (secure context for dev).
    Non-local origins must match ``site_url`` (if provided) or
    ``config.app.public_base_url``.
    """
    raw_origin = (request.headers.get("origin") or "").strip()
    if not raw_origin:
        referer = (request.headers.get("referer") or "").strip()
        if referer:
            raw_origin = referer
    if not raw_origin:
        raw_origin = (site_url or "").strip()
    if not raw_origin:
        raw_origin = config.app.public_base_url.strip()

    origin = normalize_site_origin(raw_origin)
    if not origin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法识别当前 WebAuthn 来源",
        )

    if is_local_site_url(origin):
        return origin

    if not is_secure_context_url(origin):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="通行密钥仅支持 HTTPS；本地开发环境可使用 localhost",
        )

    resolved_site_url = (site_url or "").strip() or config.app.public_base_url.strip()
    if not resolved_site_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非本地环境使用通行密钥需要先配置站点地址",
        )

    configured_origin = normalize_site_origin(resolved_site_url)
    if not configured_origin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="配置的站点地址不是有效的 HTTP(S) 地址",
        )
    if not is_secure_context_url(configured_origin):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="配置的站点地址必须使用 HTTPS 才能启用通行密钥",
        )
    if origin != configured_origin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前来源与配置的站点地址（协议、域名或端口）不匹配",
        )

    return origin


def resolve_passkey_rp_id(origin: str) -> str:
    """Extract the RP ID (hostname) from a validated origin."""
    parsed = urlsplit(origin)
    if not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法识别当前 WebAuthn 域名",
        )
    return parsed.hostname

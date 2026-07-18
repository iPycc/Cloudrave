"""Shared helpers for resolving WebAuthn origin / rp_id."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from config import CloudraveConfig


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
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        referer = (request.headers.get("referer") or "").strip()
        if referer:
            parsed = urlsplit(referer)
            origin = f"{parsed.scheme}://{parsed.netloc}"
    if not origin:
        origin = (site_url or "").strip()
    if not origin:
        origin = config.app.public_base_url.strip()
    if not origin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法识别当前 WebAuthn 来源",
        )

    parsed_origin = urlsplit(origin)
    if parsed_origin.hostname in ("localhost", "127.0.0.1"):
        return origin.rstrip("/")

    resolved_site_url = (site_url or "").strip() or config.app.public_base_url.strip()
    if not resolved_site_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非本地环境使用通行密钥需要先配置站点地址",
        )

    parsed_site = urlsplit(resolved_site_url)
    if parsed_origin.hostname != parsed_site.hostname or parsed_origin.port != parsed_site.port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前来源与配置的站点地址不匹配",
        )

    return origin.rstrip("/")


def resolve_passkey_rp_id(origin: str) -> str:
    """Extract the RP ID (hostname) from a validated origin."""
    parsed = urlsplit(origin)
    if not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法识别当前 WebAuthn 域名",
        )
    return parsed.hostname

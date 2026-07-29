"""Website settings service."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from repositories.site_settings import SiteSettingsRepository
from schemas.site import WebsiteSettings


def _first_header_value(value: str | None) -> str:
    """Return the first value from a potentially comma-separated proxy header."""

    return (value or "").split(",", 1)[0].strip()


def normalize_site_origin(value: str | None) -> str | None:
    """Normalize an HTTP(S) URL to the browser origin used by WebAuthn."""

    candidate = (value or "").strip()
    if not candidate:
        return None

    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None

    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def site_url_scheme(value: str | None) -> str:
    origin = normalize_site_origin(value)
    return urlsplit(origin).scheme if origin else ""


def site_url_hostname(value: str | None) -> str:
    origin = normalize_site_origin(value)
    return (urlsplit(origin).hostname or "") if origin else ""


def is_local_site_url(value: str | None) -> bool:
    hostname = site_url_hostname(value)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def is_secure_context_url(value: str | None) -> bool:
    """Match the browser secure-context rule relevant to WebAuthn."""

    return site_url_scheme(value) == "https" or is_local_site_url(value)


def _forwarded_origin(request: Request) -> str | None:
    raw = _first_header_value(request.headers.get("forwarded"))
    if not raw:
        return None

    values: dict[str, str] = {}
    for part in raw.split(";"):
        key, separator, value = part.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip().strip('"')

    proto = values.get("proto", "")
    host = values.get("host", "")
    return normalize_site_origin(f"{proto}://{host}") if proto and host else None


def detect_site_url(request: Request) -> str:
    """Build the current site URL from proxy headers or the request itself.

    The detection order is:
    1. Standard or de-facto proxy headers with both scheme and host
    2. Browser ``Origin`` / ``Referer`` (the public URL seen by the user)
    3. A forwarded scheme combined with the available host
    4. ``Host`` header with the request scheme
    5. FastAPI's ``request.base_url``
    """

    standard_forwarded = _forwarded_origin(request)
    if standard_forwarded:
        return standard_forwarded

    forwarded_proto = _first_header_value(request.headers.get("x-forwarded-proto")).lower()
    forwarded_host = _first_header_value(request.headers.get("x-forwarded-host"))
    host = _first_header_value(request.headers.get("host"))

    if forwarded_proto and forwarded_host:
        forwarded = normalize_site_origin(f"{forwarded_proto}://{forwarded_host}")
        if forwarded:
            return forwarded

    origin = normalize_site_origin(request.headers.get("origin"))
    if origin:
        return origin

    referer = normalize_site_origin(request.headers.get("referer"))
    if referer:
        return referer

    if forwarded_proto:
        forwarded = normalize_site_origin(f"{forwarded_proto}://{forwarded_host or host}")
        if forwarded:
            return forwarded

    if forwarded_host:
        forwarded = normalize_site_origin(f"{request.url.scheme}://{forwarded_host}")
        if forwarded:
            return forwarded

    if host:
        direct = normalize_site_origin(f"{request.url.scheme}://{host}")
        if direct:
            return direct

    return normalize_site_origin(str(request.base_url)) or str(request.base_url).rstrip("/")


class WebsiteSettingsService:
    def __init__(self, repository: SiteSettingsRepository) -> None:
        self.repository = repository

    async def get(self) -> WebsiteSettings:
        raw = await self.repository.get_settings()
        return WebsiteSettings.model_validate(raw)

    async def update(self, payload: WebsiteSettings) -> WebsiteSettings:
        changes = payload.model_dump(exclude_unset=True)
        if "site_url" in changes:
            requested_site_url = str(changes["site_url"] or "").strip()
            normalized_site_url = normalize_site_origin(requested_site_url)
            if requested_site_url and normalized_site_url is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="网站地址必须是包含 http:// 或 https:// 的有效地址",
                )

            current = await self.repository.get_settings()
            current_origin = normalize_site_origin(str(current.get("site_url") or ""))
            if (
                normalized_site_url
                and not is_secure_context_url(normalized_site_url)
                and normalized_site_url != current_origin
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="非本地网站地址必须使用 HTTPS；HTTP 无法安全使用通行密钥并会暴露密码和验证码",
                )
            changes["site_url"] = normalized_site_url or ""

        raw = await self.repository.update_settings(changes)
        return WebsiteSettings.model_validate(raw)

    async def get_site_url(self) -> str:
        """Return the configured site URL, if any."""

        settings = await self.get()
        return settings.site_url.strip()

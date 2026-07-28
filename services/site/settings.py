"""Website settings service."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from repositories.site_settings import SiteSettingsRepository
from schemas.site import WebsiteSettings


def detect_site_url(request: Request) -> str:
    """Build the current site URL from proxy headers or the request itself.

    The detection order is:
    1. ``X-Forwarded-Proto`` + ``X-Forwarded-Host``
    2. ``Host`` header with the request scheme
    3. ``Origin`` header
    4. ``Referer`` header
    5. FastAPI's ``request.base_url``
    """

    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").strip()
    host = (request.headers.get("host") or "").strip()

    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme
        return f"{scheme}://{forwarded_host}"

    if host:
        return f"{request.url.scheme}://{host}"

    origin = (request.headers.get("origin") or "").strip()
    if origin:
        return origin.rstrip("/")

    referer = (request.headers.get("referer") or "").strip()
    if referer:
        parsed = urlsplit(referer)
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    return str(request.base_url).rstrip("/")


class WebsiteSettingsService:
    def __init__(self, repository: SiteSettingsRepository) -> None:
        self.repository = repository

    async def get(self) -> WebsiteSettings:
        raw = await self.repository.get_settings()
        return WebsiteSettings.model_validate(raw)

    async def update(self, payload: WebsiteSettings) -> WebsiteSettings:
        raw = await self.repository.update_settings(payload.model_dump())
        return WebsiteSettings.model_validate(raw)

    async def get_site_url(self) -> str:
        """Return the configured site URL, if any."""

        settings = await self.get()
        return settings.site_url.strip()

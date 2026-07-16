"""Website settings service."""

from __future__ import annotations

from app.repositories.site_settings import SiteSettingsRepository
from app.schemas.site import WebsiteSettings


class WebsiteSettingsService:
    def __init__(self, repository: SiteSettingsRepository) -> None:
        self.repository = repository

    async def get(self) -> WebsiteSettings:
        raw = await self.repository.get_settings()
        return WebsiteSettings.model_validate(raw)

    async def update(self, payload: WebsiteSettings) -> WebsiteSettings:
        raw = await self.repository.update_settings(payload.model_dump())
        return WebsiteSettings.model_validate(raw)

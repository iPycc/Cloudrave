"""Website settings persistence using system_meta key-value store."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import SystemMeta


SITE_SETTINGS_KEY = "site.settings"


class SiteSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_settings(self) -> dict[str, object]:
        row = await self.session.scalar(select(SystemMeta).where(SystemMeta.key == SITE_SETTINGS_KEY))
        if row is None or not row.value:
            return {}
        try:
            parsed = json.loads(row.value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {}

    async def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        current = await self.get_settings()
        current.update(payload)
        row = await self.session.scalar(select(SystemMeta).where(SystemMeta.key == SITE_SETTINGS_KEY))
        if row is None:
            row = SystemMeta(key=SITE_SETTINGS_KEY, value=json.dumps(current, ensure_ascii=False))
            self.session.add(row)
        else:
            row.value = json.dumps(current, ensure_ascii=False)
        await self.session.flush()
        return current

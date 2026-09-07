"""GitHub OAuth configuration and external identity persistence."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import OAuthIdentity, SystemMeta, utcnow


GITHUB_OAUTH_SETTINGS_KEY = "oauth.github.settings"


class GitHubOAuthSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> dict[str, object]:
        row = await self.session.scalar(
            select(SystemMeta).where(SystemMeta.key == GITHUB_OAUTH_SETTINGS_KEY)
        )
        if row is None or not row.value:
            return {}
        try:
            value = json.loads(row.value)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    async def update(self, value: dict[str, object]) -> dict[str, object]:
        row = await self.session.scalar(
            select(SystemMeta).where(SystemMeta.key == GITHUB_OAUTH_SETTINGS_KEY)
        )
        serialized = json.dumps(value, ensure_ascii=False)
        if row is None:
            self.session.add(SystemMeta(key=GITHUB_OAUTH_SETTINGS_KEY, value=serialized))
        else:
            row.value = serialized
        await self.session.flush()
        return value


class OAuthIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, provider: str, subject: str) -> OAuthIdentity | None:
        return await self.session.scalar(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == provider,
                OAuthIdentity.subject == subject,
            )
        )

    async def get_for_user(self, user_id: int, provider: str) -> OAuthIdentity | None:
        return await self.session.scalar(
            select(OAuthIdentity).where(
                OAuthIdentity.user_id == user_id,
                OAuthIdentity.provider == provider,
            )
        )

    async def create(
        self,
        *,
        user_id: int,
        provider: str,
        subject: str,
        provider_username: str | None,
        provider_email: str | None,
    ) -> OAuthIdentity:
        identity = OAuthIdentity(
            user_id=user_id,
            provider=provider,
            subject=subject,
            provider_username=provider_username,
            provider_email=provider_email,
        )
        self.session.add(identity)
        await self.session.flush()
        return identity

    async def touch(
        self,
        identity: OAuthIdentity,
        *,
        provider_username: str | None,
        provider_email: str | None,
    ) -> OAuthIdentity:
        identity.provider_username = provider_username
        identity.provider_email = provider_email
        identity.last_used_at = utcnow()
        await self.session.flush()
        return identity

    async def delete(self, identity: OAuthIdentity) -> None:
        await self.session.delete(identity)
        await self.session.flush()

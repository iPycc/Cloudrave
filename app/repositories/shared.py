"""Mounted share persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.shared import ShareMount


class SharedRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, mount_id: int, user_id: int) -> ShareMount | None:
        return await self.session.scalar(
            select(ShareMount).where(ShareMount.id == mount_id, ShareMount.user_id == user_id)
        )

    async def get_share(self, user_id: int, share_id: str) -> ShareMount | None:
        return await self.session.scalar(
            select(ShareMount).where(ShareMount.user_id == user_id, ShareMount.share_id == share_id)
        )

    async def list(self, user_id: int) -> list[ShareMount]:
        result = await self.session.scalars(
            select(ShareMount)
            .where(ShareMount.user_id == user_id)
            .order_by(ShareMount.created_at.desc(), ShareMount.id.desc())
        )
        return list(result)

    async def create(self, user_id: int, share_id: str, *, grant_hash: str | None = None) -> ShareMount:
        mount = ShareMount(user_id=user_id, share_id=share_id, grant_hash=grant_hash)
        self.session.add(mount)
        await self.session.flush()
        return mount

    async def delete(self, mount: ShareMount) -> None:
        await self.session.delete(mount)
        await self.session.flush()

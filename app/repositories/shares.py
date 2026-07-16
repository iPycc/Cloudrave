"""Share record persistence operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ShareAccess
from app.models.tables import ShareRecord


class ShareRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, share_id: str) -> ShareRecord | None:
        return await self.session.get(ShareRecord, share_id)

    async def list_for_owner(self, owner_id: int) -> list[ShareRecord]:
        result = await self.session.scalars(
            select(ShareRecord)
            .where(ShareRecord.owner_id == owner_id)
            .order_by(ShareRecord.created_at.desc())
        )
        return list(result)

    async def create(
        self,
        *,
        owner_id: int,
        node_id: int,
        access: ShareAccess,
        password_hash: str | None,
        expires_at: datetime | None,
        max_downloads: int | None,
    ) -> ShareRecord:
        share = ShareRecord(
            id=str(uuid4()),
            owner_id=owner_id,
            node_id=node_id,
            access=access,
            password_hash=password_hash,
            expires_at=expires_at,
            max_downloads=max_downloads,
            download_count=0,
            view_count=0,
            is_active=True,
        )
        self.session.add(share)
        await self.session.flush()
        return share

    async def increment_view(self, share: ShareRecord) -> None:
        share.view_count += 1
        await self.session.flush()

    async def increment_download(self, share: ShareRecord) -> None:
        share.download_count += 1
        if share.max_downloads is not None and share.download_count >= share.max_downloads:
            share.is_active = False
        await self.session.flush()

    async def delete(self, share: ShareRecord) -> None:
        await self.session.delete(share)
        await self.session.flush()

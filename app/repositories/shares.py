"""Share record persistence operations."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ShareAccess
from app.models.tables import ShareRecord

_SHARE_ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
_SHARE_ID_LENGTH = 6


def _generate_share_id() -> str:
    return "".join(secrets.choice(_SHARE_ID_CHARS) for _ in range(_SHARE_ID_LENGTH))


class ShareRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, share_id: str) -> ShareRecord | None:
        return await self.session.get(ShareRecord, share_id)

    async def list_for_owner(self, owner_id: int) -> list[ShareRecord]:
        result = await self.session.scalars(
            select(ShareRecord)
            .where(ShareRecord.owner_id == owner_id)
            .options(selectinload(ShareRecord.node))
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
        for _ in range(10):
            share_id = _generate_share_id()
            existing = await self.session.get(ShareRecord, share_id)
            if existing is None:
                break
        else:
            share_id = _generate_share_id()

        share = ShareRecord(
            id=share_id,
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

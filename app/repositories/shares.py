"""Share record persistence operations."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import ShareAccess
from app.models.tables import ShareItem, ShareRecord

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
            .options(selectinload(ShareRecord.node), selectinload(ShareRecord.items))
            .order_by(ShareRecord.created_at.desc())
        )
        return list(result)

    async def list_by_ids(self, share_ids: list[str]) -> list[ShareRecord]:
        if not share_ids:
            return []
        result = await self.session.scalars(
            select(ShareRecord)
            .where(ShareRecord.id.in_(share_ids))
            .options(selectinload(ShareRecord.items))
        )
        return list(result)

    async def create(
        self,
        *,
        owner_id: int,
        node_ids: list[int],
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
            node_id=node_ids[0],
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
        self.session.add_all(
            ShareItem(share_id=share.id, node_id=node_id, position=position)
            for position, node_id in enumerate(node_ids)
        )
        await self.session.flush()
        return share

    async def list_root_node_ids(self, share: ShareRecord) -> list[int]:
        result = await self.session.scalars(
            select(ShareItem.node_id)
            .where(ShareItem.share_id == share.id)
            .order_by(ShareItem.position.asc(), ShareItem.id.asc())
        )
        node_ids = list(result)
        return node_ids or [share.node_id]

    async def increment_view(self, share: ShareRecord) -> None:
        await self.session.execute(
            update(ShareRecord)
            .where(ShareRecord.id == share.id, ShareRecord.is_active.is_(True))
            .values(view_count=ShareRecord.view_count + 1)
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        await self.session.refresh(share)

    async def increment_download(self, share: ShareRecord) -> bool:
        """Atomically reserve one download without overshooting the limit."""

        result = await self.session.execute(
            update(ShareRecord)
            .where(
                ShareRecord.id == share.id,
                ShareRecord.is_active.is_(True),
                or_(ShareRecord.max_downloads.is_(None), ShareRecord.download_count < ShareRecord.max_downloads),
            )
            .values(
                download_count=ShareRecord.download_count + 1,
                is_active=case(
                    (
                        and_(
                            ShareRecord.max_downloads.is_not(None),
                            ShareRecord.download_count + 1 >= ShareRecord.max_downloads,
                        ),
                        False,
                    ),
                    else_=ShareRecord.is_active,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        consumed = bool(result.rowcount)
        if consumed:
            await self.session.refresh(share)
        return consumed

    async def delete(self, share: ShareRecord) -> None:
        await self.session.delete(share)
        await self.session.flush()

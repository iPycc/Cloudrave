"""Session persistence operations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import SessionStatus
from models.tables import Session


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: int,
        refresh_token_hash: str,
        refresh_expires_at: datetime,
        ip: str | None,
        user_agent: str | None,
    ) -> Session:
        record = Session(
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            refresh_expires_at=refresh_expires_at,
            ip=ip,
            user_agent=user_agent,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_active_by_id(self, session_id: str) -> Session | None:
        stmt = select(Session).where(
            Session.id == session_id,
            Session.revoked_at.is_(None),
            Session.status == SessionStatus.ACTIVE,
        )
        return await self.session.scalar(stmt)

    async def touch(self, session_id: str) -> None:
        await self.session.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(last_seen_at=datetime.now(UTC))
        )

    async def revoke_all_for_user(self, user_id: int) -> int:
        now = datetime.now(UTC)
        result = await self.session.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=now, status=SessionStatus.REVOKED)
        )
        return result.rowcount or 0

    async def revoke_by_id(self, session_id: str) -> int:
        now = datetime.now(UTC)
        result = await self.session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=now, status=SessionStatus.REVOKED)
        )
        return result.rowcount or 0

    async def rotate_refresh_token(
        self,
        session_id: str,
        *,
        refresh_token_hash: str,
        refresh_expires_at: datetime,
    ) -> None:
        await self.session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(
                refresh_token_hash=refresh_token_hash,
                refresh_expires_at=refresh_expires_at,
                token_version=Session.token_version + 1,
                last_seen_at=datetime.now(UTC),
            )
        )

"""Resolve and validate the authenticated user/session pair in one query."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import SessionStatus, UserRole, UserStatus
from models.tables import Session as SessionRecord, User
from repositories.mounts import BucketMountRepository
from repositories.sessions import SessionRepository
from services.user.guest_access import guest_is_available


class AuthenticatedSessionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        sessions: SessionRepository,
        mounts: BucketMountRepository,
    ) -> None:
        self.session = session
        self.sessions = sessions
        self.mounts = mounts

    async def resolve(self, *, user_uid: str, session_id: str, token_version: int) -> tuple[User, SessionRecord]:
        row = (
            await self.session.execute(
                select(User, SessionRecord)
                .join(SessionRecord, SessionRecord.user_id == User.id)
                .where(
                    User.uid == user_uid,
                    SessionRecord.id == session_id,
                    SessionRecord.status == SessionStatus.ACTIVE,
                )
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
        user, session_record = row
        if user.status != UserStatus.ACTIVE:
            await self.sessions.revoke_all_for_user(user.id)
            await self.session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account disabled")
        if user.role == UserRole.GUEST and not await guest_is_available(user, self.mounts):
            await self.sessions.revoke_all_for_user(user.id)
            await self.session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Guest account expired")
        if session_record.token_version != token_version:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session token rotated")
        return user, session_record

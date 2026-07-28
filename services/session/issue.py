"""Session issue helpers."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import Session, User
from repositories.sessions import SessionRepository
from schemas.auth import TokenPairResponse
from security.jwt import TokenService


class IssueSessionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        sessions: SessionRepository,
        token_service: TokenService,
    ) -> None:
        self.session = session
        self.sessions = sessions
        self.token_service = token_service

    async def issue_for_user(self, request: Request, user: User) -> tuple[Session, TokenPairResponse]:
        temp_bundle = self.token_service.issue_token_pair(
            user_uid=user.uid,
            role=user.role.value,
            session_id="pending",
            token_version=1,
        )
        session_record = await self.sessions.create(
            user_id=user.id,
            refresh_token_hash=self.token_service.hash_refresh_token(temp_bundle.refresh_token),
            refresh_expires_at=temp_bundle.refresh_expires_at,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        bundle = self.token_service.issue_token_pair(
            user_uid=user.uid,
            role=user.role.value,
            session_id=session_record.id,
            token_version=session_record.token_version,
        )
        session_record.refresh_token_hash = self.token_service.hash_refresh_token(bundle.refresh_token)
        session_record.refresh_expires_at = bundle.refresh_expires_at
        await self.session.flush()
        return session_record, TokenPairResponse(
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            access_expires_at=bundle.access_expires_at,
            refresh_expires_at=bundle.refresh_expires_at,
            session_id=bundle.session_id,
        )


__all__ = ["IssueSessionService"]

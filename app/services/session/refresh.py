"""Refresh token rotation service."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException, status
from fastapi.requests import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserStatus
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.schemas.auth import RefreshRequest, TokenPairResponse
from app.security.jwt import AuthError, TokenService


LOGGER = logging.getLogger("cloudrave.auth")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class RefreshTokenService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        audits: AuditLogRepository,
        token_service: TokenService,
    ) -> None:
        self.session = session
        self.users = users
        self.sessions = sessions
        self.audits = audits
        self.token_service = token_service

    async def refresh(self, request: Request, payload: RefreshRequest) -> TokenPairResponse:
        try:
            session_id, _secret = self.token_service.parse_refresh_token(payload.refresh_token)
        except AuthError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        session_record = await self.sessions.get_active_by_id(session_id)
        if session_record is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
        if _as_utc(session_record.refresh_expires_at) <= datetime.now(UTC):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

        expected_hash = self.token_service.hash_refresh_token(payload.refresh_token)
        if expected_hash != session_record.refresh_token_hash:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token mismatch")

        user = await self.users.get_by_id(session_record.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        if user.status != UserStatus.ACTIVE:
            await self.sessions.revoke_all_for_user(user.id)
            await self.session.commit()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account disabled")

        next_version = session_record.token_version + 1
        bundle = self.token_service.issue_token_pair(
            user_uid=user.uid,
            role=user.role.value,
            session_id=session_record.id,
            token_version=next_version,
        )
        await self.sessions.rotate_refresh_token(
            session_record.id,
            refresh_token_hash=self.token_service.hash_refresh_token(bundle.refresh_token),
            refresh_expires_at=bundle.refresh_expires_at,
        )
        await self.audits.create(
            actor_id=user.id,
            action="refresh",
            target=f"session:{session_record.id}",
            detail=f"token_version={next_version}",
            ip=request.client.host if request.client else session_record.ip,
            user_agent=request.headers.get("user-agent") or session_record.user_agent,
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Rotated session {session_record.id} to token version {next_version}",
            extra={
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": user.email,
            },
        )
        return TokenPairResponse(
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            access_expires_at=bundle.access_expires_at,
            refresh_expires_at=bundle.refresh_expires_at,
            session_id=bundle.session_id,
        )

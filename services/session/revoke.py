"""Session revocation service."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.audit_logs import AuditLogRepository
from repositories.sessions import SessionRepository
from repositories.users import UserRepository
from schemas.auth import LogoutRequest
from security.jwt import AuthError, TokenService
from core.log import LogOperation, LogOutcome


LOGGER = logging.getLogger("cloudrave.auth")


class RevokeSessionService:
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

    async def revoke(self, request: Request, payload: LogoutRequest) -> dict[str, int | str]:
        try:
            session_id, _secret = self.token_service.parse_refresh_token(payload.refresh_token)
        except AuthError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        session_record = await self.sessions.get_active_by_id(session_id)
        if session_record is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
        if self.token_service.hash_refresh_token(payload.refresh_token) != session_record.refresh_token_hash:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token mismatch")

        user = await self.users.get_by_id(session_record.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

        if payload.scope == "all":
            revoked = await self.sessions.revoke_all_for_user(user.id)
        else:
            revoked = await self.sessions.revoke_by_id(session_record.id)
        await self.audits.create(
            actor_id=user.id,
            action="logout",
            target=f"user:{user.id}" if payload.scope == "all" else f"session:{session_record.id}",
            detail=f"scope={payload.scope}; revoked_sessions={revoked}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            "User logged out; revoked %s session(s)",
            revoked,
            extra={
                "operation": LogOperation.LOGOUT,
                "resource": "session",
                "outcome": LogOutcome.SUCCESS,
                "scope": payload.scope,
                "count": revoked,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": user.email,
            },
        )
        return {"revoked_sessions": revoked, "scope": payload.scope}

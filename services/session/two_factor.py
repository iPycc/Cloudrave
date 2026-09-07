"""Complete login after TOTP verification."""

from __future__ import annotations

import json

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import User
from repositories.audit_logs import AuditLogRepository
from repositories.sessions import SessionRepository
from repositories.users import UserRepository
from schemas.auth import LoginResponse
from security.jwt import TokenService
from services.session.issue import IssueSessionService
from services.user.response import build_user_response
from services.user.totp import TOTPService
from core.cfg import CloudraveConfig
from web.client_ip import get_client_ip


class TwoFactorLoginService:
    """Verify a pending 2FA login and issue the final session."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        audits: AuditLogRepository,
        token_service: TokenService,
        config: CloudraveConfig,
    ) -> None:
        self.session = session
        self.users = users
        self.sessions = sessions
        self.audits = audits
        self.token_service = token_service
        self.config = config

    async def verify(
        self,
        request: Request,
        login_token: str,
        code: str,
        login_method: str,
    ) -> LoginResponse:
        totp_service = TOTPService(
            session=self.session,
            users=self.users,
            audits=self.audits,
            passkeys=None,
            password_service=None,
            token_service=self.token_service,
            config=self.config,
        )
        user = await totp_service.verify_login(request, login_token, code, login_method)

        session_record, token_pair = await IssueSessionService(
            session=self.session,
            sessions=self.sessions,
            token_service=self.token_service,
        ).issue_for_user(request, user)

        await self._record_login_attempt(request, user, login_method, "success", session_record.id)

        return LoginResponse(user=build_user_response(user), token=token_pair)

    async def _record_login_attempt(
        self,
        request: Request,
        user: User,
        login_method: str,
        result: str,
        session_id: str | None,
    ) -> None:
        await self.audits.create(
            actor_id=user.id,
            action="login",
            target=user.email,
            detail=json.dumps(
                {
                    "method": f"{_login_method_label(login_method)}两步验证登录",
                    "result": result,
                    "identifier": user.email,
                    "device_fingerprint": _build_device_fingerprint(request),
                    "session_id": session_id,
                },
                ensure_ascii=False,
            ),
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )


def _login_method_label(value: str) -> str:
    return {
        "password": "密码",
        "passkey": "通行密钥",
        "github": "GitHub ",
        "google": "Google ",
    }.get(value, value)


def _build_device_fingerprint(request: Request) -> str:
    fingerprint = request.headers.get("x-device-fingerprint")
    if fingerprint:
        return fingerprint
    return request.headers.get("user-agent", "Unknown device")

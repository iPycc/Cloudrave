
from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import UserRole, UserStatus
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.sessions import SessionRepository
from repositories.users import UserRepository
from schemas.auth import LoginRequest, LoginResponse
from security.jwt import TokenService
from security.password import PasswordService
from services.session.issue import IssueSessionService
from services.session.two_factor_token import TwoFactorTokenService
from services.user.guest_access import guest_is_available
from services.user.response import build_user_response
from core.log import LogOperation, LogOutcome
from web.client_ip import get_client_ip


LOGGER = logging.getLogger("cloudrave.auth")


class UserLoginService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        audits: AuditLogRepository,
        mounts: BucketMountRepository,
        password_service: PasswordService,
        token_service: TokenService,
    ) -> None:
        self.session = session
        self.users = users
        self.sessions = sessions
        self.audits = audits
        self.mounts = mounts
        self.password_service = password_service
        self.token_service = token_service

    async def login(self, request: Request, payload: LoginRequest) -> LoginResponse:
        normalized_email = payload.email.lower()
        user = await self.users.get_by_email(normalized_email)
        password_matches = bool(user and self.password_service.verify_password(payload.password, user.password_hash))

        if not password_matches:
            await self._record_login_attempt(
                request,
                actor_id=user.id if user else None,
                identifier=normalized_email,
                result="failure",
            )
            await self.session.commit()
            LOGGER.warning(
                "Login denied for %s: invalid credentials",
                normalized_email,
                extra={
                    "operation": LogOperation.LOGIN,
                    "resource": "session",
                    "outcome": LogOutcome.DENIED,
                    "request_id": getattr(request.state, "request_id", "-"),
                    "ip": request.client.host if request.client else "-",
                    "actor": normalized_email,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="邮箱或密码错误",
            )

        if user.status != UserStatus.ACTIVE:
            await self._record_login_attempt(
                request,
                actor_id=user.id,
                identifier=normalized_email,
                result="disabled",
            )
            await self.session.commit()
            LOGGER.warning(
                "Login denied for %s: account disabled",
                normalized_email,
                extra={
                    "operation": LogOperation.LOGIN,
                    "resource": "session",
                    "outcome": LogOutcome.DENIED,
                    "request_id": getattr(request.state, "request_id", "-"),
                    "ip": request.client.host if request.client else "-",
                    "actor": user.uid,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="账号已被禁用",
            )

        if user.role == UserRole.GUEST:
            if not await guest_is_available(user, self.mounts):
                await self.sessions.revoke_all_for_user(user.id)
                await self._record_login_attempt(
                    request,
                    actor_id=user.id,
                    identifier=normalized_email,
                    result="expired",
                )
                await self.session.commit()
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Guest account is unavailable or expired")

        if user.totp_enabled and user.totp_secret:
            two_factor_token = TwoFactorTokenService(self.token_service).issue_login_token(
                user.uid,
                login_method="password",
            )
            return LoginResponse(
                user=build_user_response(user),
                token=None,
                requires_2fa=True,
                two_factor_token=two_factor_token,
                message="请输入两步验证码",
            )

        session_record, token_pair = await IssueSessionService(
            session=self.session,
            sessions=self.sessions,
            token_service=self.token_service,
        ).issue_for_user(request, user)

        await self._record_login_attempt(
            request,
            actor_id=user.id,
            identifier=normalized_email,
            result="success",
            session_id=session_record.id,
        )
        LOGGER.info(
            "User logged in with session %s",
            session_record.id,
            extra={
                "operation": LogOperation.LOGIN,
                "resource": "session",
                "outcome": LogOutcome.SUCCESS,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": user.uid,
            },
        )
        return LoginResponse(user=build_user_response(user), token=token_pair)

    async def _record_login_attempt(
        self,
        request: Request,
        *,
        actor_id: int | None,
        identifier: str,
        result: str,
        session_id: str | None = None,
    ) -> None:
        await self.audits.create(
            actor_id=actor_id,
            action="login",
            target=identifier,
            detail=json.dumps(
                {
                    "method": "密码登录",
                    "result": result,
                    "identifier": identifier,
                    "device_fingerprint": _build_device_fingerprint(request),
                    "session_id": session_id,
                },
                ensure_ascii=False,
            ),
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )


def _build_device_fingerprint(request: Request) -> str:
    fingerprint = request.headers.get("x-device-fingerprint")
    if fingerprint:
        return fingerprint
    return request.headers.get("user-agent", "Unknown device")

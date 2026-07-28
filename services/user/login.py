
from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import UserStatus
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.sessions import SessionRepository
from repositories.users import UserRepository
from schemas.auth import LoginRequest, LoginResponse
from security.jwt import TokenService
from security.password import PasswordService
from services.session.issue import IssueSessionService
from services.user.response import build_user_response
from services.user.storage import UserStorageProvisionService
from services.user.totp import TOTPService
from store.manager import StorageManager
from core.cfg import CloudraveConfig
from core.log import LogOperation, LogOutcome


LOGGER = logging.getLogger("cloudrave.auth")


class UserLoginService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        users: UserRepository,
        sessions: SessionRepository,
        audits: AuditLogRepository,
        policies: StoragePolicyRepository,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        password_service: PasswordService,
        token_service: TokenService,
        storage_manager: StorageManager,
        config: CloudraveConfig,
    ) -> None:
        self.session = session
        self.users = users
        self.sessions = sessions
        self.audits = audits
        self.policies = policies
        self.mounts = mounts
        self.nodes = nodes
        self.password_service = password_service
        self.token_service = token_service
        self.storage_manager = storage_manager
        self.config = config

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

        await UserStorageProvisionService(
            policies=self.policies,
            mounts=self.mounts,
            nodes=self.nodes,
            storage_manager=self.storage_manager,
        ).ensure_default_local_storage(user)

        if user.totp_enabled and user.totp_secret:
            two_factor_token = TOTPService(
                session=self.session,
                users=self.users,
                audits=self.audits,
                passkeys=None,  # type: ignore[arg-type]
                password_service=self.password_service,
                token_service=self.token_service,
                config=self.config,
            ).issue_login_token(user.uid, login_method="password")
            return LoginResponse(
                user=build_user_response(user),
                token=None,  # type: ignore[arg-type]
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
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )


def _build_device_fingerprint(request: Request) -> str:
    fingerprint = request.headers.get("x-device-fingerprint")
    if fingerprint:
        return fingerprint
    return request.headers.get("user-agent", "Unknown device")

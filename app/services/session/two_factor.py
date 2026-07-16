"""Complete login after TOTP verification."""

from __future__ import annotations

import json

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.policies import StoragePolicyRepository
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.schemas.auth import LoginResponse
from app.security.jwt import TokenService
from app.services.session.issue import IssueSessionService
from app.services.user.response import build_user_response
from app.services.user.storage import UserStorageProvisionService
from app.services.user.totp import TOTPService
from app.storage.manager import StorageManager
from config import CloudraveConfig


class TwoFactorLoginService:
    """Verify a pending 2FA login and issue the final session."""

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
        self.token_service = token_service
        self.storage_manager = storage_manager
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
            passkeys=None,  # type: ignore[arg-type]
            password_service=None,  # type: ignore[arg-type]
            token_service=self.token_service,
            config=self.config,
        )
        user = await totp_service.verify_login(request, login_token, code, login_method)

        await UserStorageProvisionService(
            policies=self.policies,
            mounts=self.mounts,
            nodes=self.nodes,
            storage_manager=self.storage_manager,
        ).ensure_default_local_storage(user)

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
                    "method": f"{login_method}两步验证登录",
                    "result": result,
                    "identifier": user.email,
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

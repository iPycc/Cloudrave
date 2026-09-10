from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.sessions import SessionRepository
from repositories.users import UserRepository
from schemas.auth import LoginResponse, RegisterRequest
from security.jwt import TokenService
from security.password import PasswordService
from services.session.issue import IssueSessionService
from services.user.response import build_user_response
from services.user.storage import UserStorageProvisionService
from store.manager import StorageManager
from core.log import LogOperation, LogOutcome


LOGGER = logging.getLogger("cloudrave.auth")


class UserRegisterService:
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

    async def register(self, request: Request, payload: RegisterRequest) -> LoginResponse:
        existing = await self.users.get_by_email(payload.email)
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱已存在")

        try:
            user = await self.users.create_first_admin_aware(
                email=payload.email,
                username=payload.username,
                password_hash=self.password_service.hash_password(payload.password),
                timezone=payload.timezone,
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="邮箱或用户名已存在") from exc

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
        ).issue_for_user(
            request,
            user,
        )
        await self.audits.create(
            actor_id=user.id,
            action="register",
            target=f"user:{user.id}",
            detail=f"role={user.role.value}; session={session_record.id}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            "Registered user %s",
            user.email,
            extra={
                "operation": LogOperation.REGISTER,
                "resource": "user",
                "outcome": LogOutcome.SUCCESS,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": user.email,
            },
        )
        return LoginResponse(user=build_user_response(user), token=token_pair)

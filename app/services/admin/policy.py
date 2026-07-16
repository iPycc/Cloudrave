"""Storage policy admin service."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.policies import StoragePolicyRepository
from app.schemas.policy import StoragePolicyCreate, StoragePolicyRead, StoragePolicyUpdate


LOGGER = logging.getLogger("cloudrave.storage")


class SingleStoragePolicyService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        policies: StoragePolicyRepository,
        audits: AuditLogRepository,
    ) -> None:
        self.session = session
        self.policies = policies
        self.audits = audits

    async def list_policies(self) -> list[StoragePolicyRead]:
        return [self._to_schema(policy) for policy in await self.policies.list_all()]

    async def get_policy(self, policy_id: int) -> StoragePolicyRead:
        policy = await self.policies.get_by_id(policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return self._to_schema(policy)

    async def create_policy(self, request: Request, actor: User, payload: StoragePolicyCreate) -> StoragePolicyRead:
        policy = await self.policies.create(payload)
        await self.audits.create(
            actor_id=actor.id,
            action="policy.create",
            target=f"policy:{policy.id}",
            detail=policy.provider.value,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Created storage policy {policy.name}",
            extra={
                "operation": "CREATE",
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return self._to_schema(policy)

    async def update_policy(
        self,
        request: Request,
        actor: User,
        policy_id: int,
        payload: StoragePolicyUpdate,
    ) -> StoragePolicyRead:
        policy = await self.policies.get_by_id(policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        updated = await self.policies.update(policy, payload)
        await self.audits.create(
            actor_id=actor.id,
            action="policy.update",
            target=f"policy:{updated.id}",
            detail="updated",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return self._to_schema(updated)

    async def delete_policy(self, request: Request, actor: User, policy_id: int) -> None:
        policy = await self.policies.get_by_id(policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        await self.policies.delete(policy)
        await self.audits.create(
            actor_id=actor.id,
            action="policy.delete",
            target=f"policy:{policy_id}",
            detail=policy.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Deleted storage policy {policy.name}",
            extra={
                "operation": "DELETE",
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )

    def _to_schema(self, policy) -> StoragePolicyRead:
        data = StoragePolicyRead.model_validate(policy).model_dump()
        if data.get("secret_id"):
            data["secret_id"] = self._mask_secret(data["secret_id"])
        if data.get("secret_key"):
            data["secret_key"] = self._mask_secret(data["secret_key"])
        if data.get("session_token"):
            data["session_token"] = self._mask_secret(data["session_token"])
        return StoragePolicyRead.model_validate(data)

    def _mask_secret(self, value: str) -> str:
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}{'*' * max(4, len(value) - 8)}{value[-4:]}"

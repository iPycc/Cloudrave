"""Storage policy admin service."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import User
from repositories.audit_logs import AuditLogRepository
from repositories.policies import StoragePolicyRepository
from schemas.policy import StoragePolicyCreate, StoragePolicyRead, StoragePolicyUpdate
from core.log import LogOperation, LogOutcome
from store.manager import StorageManager


LOGGER = logging.getLogger("cloudrave.store")


class SingleStoragePolicyService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        policies: StoragePolicyRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.policies = policies
        self.audits = audits
        self.storage_manager = storage_manager

    async def list_policies(self) -> list[StoragePolicyRead]:
        return [self._to_schema(policy) for policy in await self.policies.list_all()]

    async def get_policy(self, policy_id: int) -> StoragePolicyRead:
        policy = await self.policies.get_by_id(policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return self._to_schema(policy)

    async def create_policy(self, request: Request, actor: User, payload: StoragePolicyCreate) -> StoragePolicyRead:
        try:
            self.storage_manager.definition_for(payload.provider)
            ciphertext, version = self.storage_manager.credentials_from_payload(payload.provider, payload)
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        policy = await self.policies.create(payload)
        policy.credential_ciphertext = ciphertext
        policy.credential_version = version
        await self.session.flush()
        await self.audits.create(
            actor_id=actor.id,
            action="policy.create",
            target=f"policy:{policy.id}",
            detail=str(policy.provider),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Created storage policy {policy.name}",
            extra={
                "operation": LogOperation.CREATE,
                "resource": "storage_policy",
                "outcome": LogOutcome.SUCCESS,
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
        mapping_fields = {"bucket_name", "region", "endpoint", "base_prefix"}
        changed_mapping = [
            field
            for field in mapping_fields.intersection(payload.model_fields_set)
            if getattr(payload, field) != getattr(policy, field)
        ]
        if changed_mapping and await self.policies.has_mounts(policy_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Policy is mounted; cannot change {', '.join(sorted(changed_mapping))}",
            )
        try:
            ciphertext, version = self.storage_manager.credentials_from_payload(
                policy.provider,
                payload,
                current=policy,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        updated = await self.policies.update(policy, payload)
        updated.credential_ciphertext = ciphertext
        updated.credential_version = version
        await self.session.flush()
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
        if await self.policies.has_mounts(policy_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Storage policy is still used by one or more mounts",
            )
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
                "operation": LogOperation.DELETE,
                "resource": "storage_policy",
                "outcome": LogOutcome.SUCCESS,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )

    def _to_schema(self, policy) -> StoragePolicyRead:
        configured, fields = self.storage_manager.credential_state(policy)
        return StoragePolicyRead.model_validate({
            **StoragePolicyRead.model_validate(policy).model_dump(),
            "credentials_configured": configured,
            "credential_fields": fields,
        })

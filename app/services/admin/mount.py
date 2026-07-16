"""Bucket mount admin service."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import StoragePolicyType
from app.models.tables import User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.policies import StoragePolicyRepository
from app.schemas.mount import BucketMountCreate, BucketMountRead, BucketMountUpdate
from app.storage.manager import StorageManager


LOGGER = logging.getLogger("cloudrave.storage")


class MountAdminService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        nodes: NodeRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.mounts = mounts
        self.policies = policies
        self.nodes = nodes
        self.audits = audits
        self.storage_manager = storage_manager

    async def list_mounts(self) -> list[BucketMountRead]:
        return [BucketMountRead.model_validate(mount) for mount in await self.mounts.list_all()]

    async def get_mount(self, mount_id: int) -> BucketMountRead:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        return BucketMountRead.model_validate(mount)

    async def create_mount(self, request: Request, actor: User, payload: BucketMountCreate) -> BucketMountRead:
        owner_id = payload.owner_id or actor.id
        policy = await self.policies.get_by_id(payload.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")

        mount = await self.mounts.create(owner_id, payload)
        await self.nodes.ensure_mount_root(owner_id=owner_id, mount_id=mount.id, name=mount.name)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            self.storage_manager.get_adapter(policy).prepare_mount_root(mount)

        await self.audits.create(
            actor_id=actor.id,
            action="mount.create",
            target=f"mount:{mount.id}",
            detail=mount.mount_slug,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Created mount {mount.name}",
            extra={
                "operation": "CREATE",
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return BucketMountRead.model_validate(mount)

    async def update_mount(
        self,
        request: Request,
        actor: User,
        mount_id: int,
        payload: BucketMountUpdate,
    ) -> BucketMountRead:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        updated = await self.mounts.update(mount, payload)
        await self.audits.create(
            actor_id=actor.id,
            action="mount.update",
            target=f"mount:{updated.id}",
            detail=updated.mount_slug,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return BucketMountRead.model_validate(updated)

    async def delete_mount(self, request: Request, actor: User, mount_id: int) -> None:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        await self.mounts.delete(mount)
        await self.audits.create(
            actor_id=actor.id,
            action="mount.delete",
            target=f"mount:{mount_id}",
            detail=mount.mount_slug,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Deleted mount {mount.name}",
            extra={
                "operation": "DELETE",
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )

    async def configure_cors(self, request: Request, actor: User, mount_id: int) -> dict[str, object]:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")

        result = self.storage_manager.get_adapter(policy).configure_cors(policy, mount)
        policy.cors_auto_configured = True
        await self.session.flush()
        await self.audits.create(
            actor_id=actor.id,
            action="mount.cors",
            target=f"mount:{mount.id}",
            detail=str(result.get("status", "configured")),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return result

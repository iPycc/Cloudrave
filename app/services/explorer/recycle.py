"""Recycle bin services."""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.schemas.node import NodeDeleteRequest, NodeRead, NodeRestoreRequest
from app.services.explorer.gc import ObjectGc
from app.storage.manager import StorageManager


class RecycleBinService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        nodes: NodeRepository,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        objects: ObjectRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.nodes = nodes
        self.mounts = mounts
        self.policies = policies
        self.objects = objects
        self.audits = audits
        self.storage_manager = storage_manager

    async def list_deleted(self, actor: User) -> list[NodeRead]:
        nodes = await self.nodes.list_deleted_for_owner(actor.id)
        return [NodeRead.model_validate(node) for node in nodes]

    async def restore_nodes(self, request: Request, actor: User, payload: NodeRestoreRequest) -> list[NodeRead]:
        for node_id in payload.node_ids:
            node = await self.nodes.get_by_id(node_id)
            if node is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if node.owner_id != actor.id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node access denied")
            await self._require_write(node.mount_id)
        restored = await self.nodes.restore(payload.node_ids)
        await self.audits.create(
            actor_id=actor.id,
            action="node.restore",
            target=f"nodes:{','.join(str(node.id) for node in restored)}",
            detail="restore",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return [NodeRead.model_validate(node) for node in restored]

    async def permanently_delete_nodes(self, request: Request, actor: User, payload: NodeDeleteRequest) -> dict[str, list[int]]:
        if not payload.hard_delete:
            payload = NodeDeleteRequest(node_ids=payload.node_ids, hard_delete=True)
        for node_id in payload.node_ids:
            node = await self.nodes.get_by_id(node_id)
            if node is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if node.owner_id != actor.id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node access denied")
            await self._require_write(node.mount_id)
        deleted_ids = await self.nodes.collect_descendant_ids(payload.node_ids)
        gc = ObjectGc(
            mounts=self.mounts,
            nodes=self.nodes,
            policies=self.policies,
            objects=self.objects,
            storage_manager=self.storage_manager,
        )
        task_ids = await gc.stage(deleted_ids)
        await self.objects.delete_by_node_ids(deleted_ids)
        await self.nodes.hard_delete(payload.node_ids)
        await self.audits.create(
            actor_id=actor.id,
            action="node.recycle_delete",
            target=f"nodes:{','.join(str(node_id) for node_id in deleted_ids)}",
            detail="recycle_delete",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        await self.session.commit()
        await gc.run(task_ids)
        await self.session.commit()
        return {"deleted_node_ids": deleted_ids}

    async def _require_write(self, mount_id: int) -> None:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None or not mount.is_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        if mount.read_only:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bucket mount is read-only")

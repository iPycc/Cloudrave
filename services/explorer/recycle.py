"""Recycle bin services."""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import NodeStatus
from models.tables import User
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from schemas.node import NodeDeleteRequest, NodeRead, NodeRestoreRequest
from schemas.task import PathBatchQueued
from services.admin.path_plan import queue_path_operation
from services.explorer.gc import ObjectGc
from services.explorer.access import MountAccessPolicy
from services.explorer.path_write import object_path_template
from store.manager import StorageManager


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
        self.mount_access = MountAccessPolicy(mounts=mounts, policies=policies)

    async def list_deleted(self, actor: User) -> list[NodeRead]:
        nodes = await self.nodes.list_deleted_for_owner(actor.id)
        return [NodeRead.model_validate(node) for node in nodes]

    async def restore_nodes(self, request: Request, actor: User, payload: NodeRestoreRequest) -> list[NodeRead] | PathBatchQueued:
        selected = {}
        for node_id in dict.fromkeys(payload.node_ids):
            node = await self.nodes.get_by_id(node_id)
            if node is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if node.owner_id != actor.id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node access denied")
            selected[node.id] = node
        mounts = {}
        for mount_id in sorted({node.mount_id for node in selected.values()}):
            mount, policy = await self.mount_access.write(actor, mount_id, lock=True)
            mounts[mount_id] = (mount, policy, object_path_template(policy, mount))
        legacy_ids, task_ids = [], []
        for node in selected.values():
            mount, policy, template = mounts[node.mount_id]
            if template is None:
                legacy_ids.append(node.id)
                continue
            node = await self.nodes.get_by_id_for_update(node.id)
            if node is None or node.status != NodeStatus.DELETED:
                raise HTTPException(status_code=409, detail="该条目已不在回收站")
            if not await self._is_restore_root(node, selected):
                continue
            task = await queue_path_operation(self.session, mount, policy, node, template, is_restore=True)
            task_ids.append(task.id)
        restored = await self.nodes.restore(legacy_ids) if legacy_ids else []
        if restored:
            await self.audits.create(
                actor_id=actor.id,
                action="node.restore",
                target=f"nodes:{','.join(str(node.id) for node in restored)}",
                detail="restore",
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
        if task_ids:
            return PathBatchQueued(task_ids=task_ids)
        return [NodeRead.model_validate(node) for node in restored]

    async def _is_restore_root(self, node, selected) -> bool:
        parent = await self.nodes.get_by_id(node.parent_id) if node.parent_id is not None else None
        if parent is not None and parent.deleted_at is None:
            return True
        seen = {node.id}
        while parent is not None and parent.deleted_at is not None and parent.id not in seen:
            if parent.id in selected:
                return False
            seen.add(parent.id)
            parent = await self.nodes.get_by_id(parent.parent_id) if parent.parent_id is not None else None
        raise HTTPException(status_code=409, detail="请先恢复上级文件夹")

    async def permanently_delete_nodes(self, request: Request, actor: User, payload: NodeDeleteRequest) -> dict[str, list[int]]:
        if not payload.hard_delete:
            payload = NodeDeleteRequest(node_ids=payload.node_ids, hard_delete=True)
        mount_ids = set()
        for node_id in payload.node_ids:
            node = await self.nodes.get_by_id(node_id)
            if node is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
            if node.owner_id != actor.id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node access denied")
            mount_ids.add(node.mount_id)
        for mount_id in sorted(mount_ids):
            await self.mount_access.write(actor, mount_id, lock=True)
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
        if task_ids:
            await gc.run(task_ids)
            await self.session.commit()
        return {"deleted_node_ids": deleted_ids}

"""Explorer file services."""

from __future__ import annotations

import logging
import re

from fastapi import HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NodeType, UserRole
from app.models.tables import BucketMount, StoragePolicy, User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.schemas.mount import BucketMountRead
from app.schemas.node import FolderCreateRequest, NodeDeleteRequest, NodeRead, NodeRenameRequest
from app.storage.manager import StorageManager


LOGGER = logging.getLogger("cloudrave.explorer")


class FileExplorerService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        nodes: NodeRepository,
        objects: ObjectRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.mounts = mounts
        self.policies = policies
        self.nodes = nodes
        self.objects = objects
        self.audits = audits
        self.storage_manager = storage_manager

    async def list_mounts(self, actor: User) -> list[BucketMountRead]:
        mounts = await (self.mounts.list_all() if actor.role == UserRole.ADMIN else self.mounts.list_for_owner(actor.id))
        return [BucketMountRead.model_validate(mount) for mount in mounts]

    async def list_children(self, actor: User, mount_id: int, parent_id: int | None = None) -> list[NodeRead]:
        mount, _policy = await self._require_mount_access(actor, mount_id)
        if parent_id is None:
            root = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
            parent_id = root.id
        nodes = await self.nodes.list_children(mount.id, parent_id, include_deleted=False)
        return [NodeRead.model_validate(node) for node in nodes]

    async def create_folder(self, request: Request, actor: User, payload: FolderCreateRequest) -> NodeRead:
        mount, _policy = await self._require_mount_access(actor, payload.mount_id)
        parent = None
        if payload.parent_id is not None:
            parent = await self.nodes.get_by_id(payload.parent_id)
            if parent is None or parent.mount_id != mount.id or parent.type != NodeType.FOLDER:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found")
        else:
            parent = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)

        # Resolve unique folder name: append (2), (3), ... if conflict
        folder_name = payload.name
        siblings = await self.nodes.list_children(mount.id, parent.id if parent else None, include_deleted=False)
        sibling_names = {s.name for s in siblings}
        if folder_name in sibling_names:
            base = re.sub(r"\s*\(\d+\)$", "", folder_name)
            counter = 2
            while f"{base} ({counter})" in sibling_names:
                counter += 1
            folder_name = f"{base} ({counter})"

        node = await self.nodes.create_folder(
            owner_id=mount.owner_id,
            mount_id=mount.id,
            parent_id=parent.id if parent else None,
            name=folder_name,
        )
        await self.audits.create(
            actor_id=actor.id,
            action="node.create_folder",
            target=f"node:{node.id}",
            detail=node.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Created folder {node.name}",
            extra={
                "operation": "CREATE",
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return NodeRead.model_validate(node)

    async def rename_node(self, request: Request, actor: User, node_id: int, payload: NodeRenameRequest) -> NodeRead:
        node = await self._require_node_access(actor, node_id)
        updated = await self.nodes.rename(node, payload.name)
        await self.audits.create(
            actor_id=actor.id,
            action="node.rename",
            target=f"node:{updated.id}",
            detail=updated.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return NodeRead.model_validate(updated)

    async def delete_nodes(self, request: Request, actor: User, payload: NodeDeleteRequest) -> dict[str, list[int] | list[NodeRead]]:
        nodes = [await self._require_node_access(actor, node_id) for node_id in payload.node_ids]
        _ = nodes
        if payload.hard_delete:
            all_ids = await self.nodes.collect_descendant_ids(payload.node_ids)
            descendants = await self.nodes.list_by_ids(all_ids)
            for node in descendants:
                if node.type != NodeType.FILE or not node.blob_path:
                    continue
                mount = await self.mounts.get_by_id(node.mount_id)
                if mount is None:
                    continue
                policy = await self.policies.get_by_id(mount.policy_id)
                if policy is None:
                    continue
                self.storage_manager.get_adapter(policy).delete_object(policy, mount, node.blob_path, hard_delete=True)
            await self.objects.delete_by_node_ids(all_ids)
            await self.nodes.hard_delete(payload.node_ids)
            await self.audits.create(
                actor_id=actor.id,
                action="node.hard_delete",
                target=f"nodes:{','.join(str(node_id) for node_id in all_ids)}",
                detail="hard_delete",
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
            LOGGER.info(
                f"Hard deleted {len(all_ids)} node(s)",
                extra={
                    "operation": "DELETE",
                    "request_id": getattr(request.state, "request_id", "-"),
                    "ip": request.client.host if request.client else "-",
                    "actor": actor.email,
                },
            )
            return {"deleted_node_ids": all_ids}

        deleted = await self.nodes.soft_delete(payload.node_ids)
        await self.audits.create(
            actor_id=actor.id,
            action="node.soft_delete",
            target=f"nodes:{','.join(str(node.id) for node in deleted)}",
            detail="soft_delete",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Soft deleted {len(deleted)} node(s)",
            extra={
                "operation": "DELETE",
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return {"nodes": [NodeRead.model_validate(node) for node in deleted]}

    async def _require_mount_access(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        if actor.role != UserRole.ADMIN and mount.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Mount access denied")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return mount, policy

    async def _require_node_access(self, actor: User, node_id: int):
        node = await self.nodes.get_by_id(node_id)
        if node is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        if actor.role != UserRole.ADMIN and node.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node access denied")
        return node

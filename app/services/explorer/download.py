"""Download services."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.models.enums import NodeType, UserRole
from app.models.tables import User
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.policies import StoragePolicyRepository


class DownloadFileService:
    def __init__(
        self,
        *,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        policies: StoragePolicyRepository,
    ) -> None:
        self.mounts = mounts
        self.nodes = nodes
        self.policies = policies

    async def resolve(self, actor: User, node_id: int):
        node = await self.nodes.get_by_id(node_id)
        if node is None or node.type != NodeType.FILE or not node.blob_path:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        if actor.role != UserRole.ADMIN and node.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="File access denied")
        mount = await self.mounts.get_by_id(node.mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return node, mount, policy

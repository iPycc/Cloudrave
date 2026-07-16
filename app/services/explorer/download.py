"""Download services."""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from fastapi.responses import RedirectResponse, StreamingResponse

from app.models.enums import NodeType, StoragePolicyType
from app.models.tables import User
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.storage.manager import StorageManager

if TYPE_CHECKING:
    from app.models.tables import BucketMount, Node, StoragePolicy


class DownloadFileService:
    def __init__(
        self,
        *,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        policies: StoragePolicyRepository,
        objects: ObjectRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.mounts = mounts
        self.nodes = nodes
        self.policies = policies
        self.objects = objects
        self.storage_manager = storage_manager

    async def resolve(self, actor: User, node_id: int) -> tuple["Node", "BucketMount", "StoragePolicy"]:
        node = await self.nodes.get_by_id(node_id)
        if node is None or node.type != NodeType.FILE or not node.blob_path:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        if node.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="File access denied")
        return await self._resolve_mount_policy(node)

    async def resolve_shared(self, node_id: int) -> tuple["Node", "BucketMount", "StoragePolicy"]:
        node = await self.nodes.get_by_id(node_id)
        if node is None or node.type != NodeType.FILE or not node.blob_path:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        return await self._resolve_mount_policy(node)

    async def _resolve_mount_policy(self, node: "Node") -> tuple["Node", "BucketMount", "StoragePolicy"]:
        mount = await self.mounts.get_by_id(node.mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return node, mount, policy

    def build_file_stream(
        self,
        node: "Node",
        mount: "BucketMount",
        policy: "StoragePolicy",
    ) -> StreamingResponse | RedirectResponse:
        adapter = self.storage_manager.get_adapter(policy)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            content_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
            stream = adapter.open_object(policy, mount, node.blob_path)
            return StreamingResponse(stream, media_type=content_type)
        signed = adapter.build_download_url(policy, mount, node.blob_path)
        return RedirectResponse(signed.url)

"""Authorize and budget writes under the mount transaction lock."""
from __future__ import annotations

from fastapi import HTTPException, status

from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from repositories.uploads import UploadRepository
from services.explorer.access import MountAccessPolicy


async def ensure_capacity(mount, additional_bytes: int, *, nodes, uploads) -> None:
    if mount.quota_bytes is None:
        return
    used = await nodes.mount_file_bytes(mount.id)
    reserved = await uploads.reserved_bytes_for_mount(mount.id)
    if used + reserved + max(0, additional_bytes) > mount.quota_bytes:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Storage quota exceeded")


class StorageWritePolicy:
    def __init__(self, session):
        self.session = session
        self.mounts = BucketMountRepository(session)
        self.policies = StoragePolicyRepository(session)

    async def check(self, actor, mount_id: int, additional_bytes: int = 0):
        mount, policy = await MountAccessPolicy(mounts=self.mounts, policies=self.policies).write(
            actor, mount_id, lock=True)
        await ensure_capacity(mount, additional_bytes, nodes=NodeRepository(self.session),
                              uploads=UploadRepository(self.session))
        return mount, policy

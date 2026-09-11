"""Single mount access policy for explorer read, write and cleanup paths."""

from __future__ import annotations

from fastapi import HTTPException, status

from models.tables import BucketMount, StoragePolicy, User
from repositories.mounts import BucketMountRepository
from repositories.policies import StoragePolicyRepository


class MountAccessPolicy:
    def __init__(self, *, mounts: BucketMountRepository, policies: StoragePolicyRepository) -> None:
        self.mounts = mounts
        self.policies = policies

    async def read(self, actor: User, mount_id: int, *, lock: bool = False) -> tuple[BucketMount, StoragePolicy]:
        mount = await (self.mounts.get_by_id_for_update(mount_id) if lock else self.mounts.get_by_id(mount_id))
        self._require_owner(actor, mount)
        if not mount.is_enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bucket mount is disabled")
        policy = await self._policy(mount)
        if policy.status != "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Storage policy is inactive")
        return mount, policy

    async def write(self, actor: User, mount_id: int, *, lock: bool = False) -> tuple[BucketMount, StoragePolicy]:
        mount, policy = await self.read(actor, mount_id, lock=lock)
        if mount.read_only:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bucket mount is read-only")
        return mount, policy

    async def cleanup(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        """Allow abort/cleanup after a mount was disabled or its policy became inactive."""

        mount = await self.mounts.get_by_id(mount_id)
        self._require_owner(actor, mount)
        return mount, await self._policy(mount)

    @staticmethod
    def _require_owner(actor: User, mount: BucketMount | None) -> None:
        if mount is None or mount.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")

    async def _policy(self, mount: BucketMount) -> StoragePolicy:
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return policy

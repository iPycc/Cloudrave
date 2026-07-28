"""Backfill canonical physical identities for upgraded databases."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from repositories.mounts import BucketMountRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from store.manager import StorageManager


class ObjectIdService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        objects: ObjectRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.mounts = mounts
        self.policies = policies
        self.objects = objects
        self.storage_manager = storage_manager

    async def fill(self, *, page_size: int = 500) -> tuple[int, int]:
        filled = 0
        failed = 0
        after_id = 0
        while True:
            rows = await self.objects.list_missing_physical_page(after_id=after_id, limit=page_size)
            if not rows:
                return filled, failed
            for record, node in rows:
                after_id = max(after_id, record.id)
                mount = await self.mounts.get_by_id(node.mount_id)
                policy = await self.policies.get_by_id(record.policy_id)
                if mount is None or policy is None:
                    failed += 1
                    continue
                try:
                    adapter = self.storage_manager.get_adapter(policy)
                    value = adapter.physical_id(policy, mount, record.blob_path)
                except (KeyError, RuntimeError, TypeError, ValueError):
                    failed += 1
                    continue
                await self.objects.save_physical(record, value)
                filled += 1
            await self.session.commit()

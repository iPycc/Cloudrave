"""Transactional object-GC outbox and retry worker."""

from __future__ import annotations

from app.models.gc import GcTask
from app.repositories.gc import GcRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.storage.call import call_adapter
from app.storage.manager import StorageManager


class ObjectGc:
    """Stage cleanup in the DB, then delete only after metadata commits."""

    def __init__(
        self,
        *,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        policies: StoragePolicyRepository,
        objects: ObjectRepository,
        storage_manager: StorageManager,
        tasks: GcRepository | None = None,
    ) -> None:
        self.mounts = mounts
        self.nodes = nodes
        self.policies = policies
        self.objects = objects
        self.storage_manager = storage_manager
        self.tasks = tasks or GcRepository(objects.session)

    async def stage(self, node_ids: list[int]) -> list[int]:
        records = await self.objects.get_by_node_ids(node_ids)
        if not records:
            return []
        nodes = {node.id: node for node in await self.nodes.list_by_ids(node_ids)}
        mount_ids = {nodes[record.node_id].mount_id for record in records if record.node_id in nodes}
        mounts = {mount.id: mount for mount in await self.mounts.list_by_ids(list(mount_ids))}
        policy_ids = {record.policy_id for record in records}
        policies = {policy.id: policy for policy in await self.policies.list_by_ids(list(policy_ids))}

        identities: dict[int, str] = {}
        for record in records:
            node = nodes.get(record.node_id)
            mount = mounts.get(node.mount_id) if node else None
            policy = policies.get(record.policy_id)
            if node is None or mount is None or policy is None:
                continue
            identity = record.physical_id
            if not identity:
                adapter = self.storage_manager.get_adapter(policy)
                identity = adapter.physical_id(policy, mount, record.blob_path)
                record.physical_id = identity
                await self.objects.session.flush()
            identities[record.id] = identity
        for identity in sorted(set(identities.values())):
            await self.objects.blobs.lock(identity)

        referenced_ids = await self.objects.referenced_physical(
            set(identities.values()),
            excluding_node_ids=node_ids,
        )
        legacy_refs = await self.objects.referenced_keys(records, excluding_node_ids=node_ids)
        candidates: dict[str, tuple[int, int, str]] = {}
        for record in records:
            identity = identities.get(record.id)
            node = nodes.get(record.node_id)
            if not identity or node is None or identity in referenced_ids:
                continue
            legacy_key = (record.policy_id, node.mount_id, record.blob_path)
            if legacy_key in legacy_refs:
                continue
            if node.mount_id in mounts and record.policy_id in policies:
                candidates.setdefault(identity, (record.policy_id, node.mount_id, record.blob_path))

        task_ids: list[int] = []
        for identity, (policy_id, mount_id, blob_path) in candidates.items():
            task = await self.tasks.create(
                policy_id=policy_id,
                mount_id=mount_id,
                blob_path=blob_path,
                physical_id=identity,
            )
            task_ids.append(task.id)
        return task_ids

    async def run(self, task_ids: list[int]) -> int:
        return await self._run(await self.tasks.claim_by_ids(task_ids))

    async def run_pending(self, *, limit: int = 100) -> int:
        return await self._run(await self.tasks.claim_pending(limit))

    async def _run(self, tasks: list[GcTask]) -> int:
        completed = 0
        for task in sorted(tasks, key=lambda item: (item.physical_id, item.id)):
            if task.state != "processing":
                continue
            blob = await self.objects.blobs.lock(task.physical_id)
            if blob.state == "deleted":
                await self.tasks.done(task)
                completed += 1
                continue
            try:
                if await self.objects.has_physical_reference(task.physical_id):
                    await self.objects.blobs.active(blob)
                    await self.tasks.skipped(task)
                    continue
                mount = await self.mounts.get_by_id(task.mount_id)
                policy = await self.policies.get_by_id(task.policy_id)
                if mount is None or policy is None:
                    raise RuntimeError("Storage policy or mount no longer exists")
                adapter = self.storage_manager.get_adapter(policy)
                current_id = adapter.physical_id(policy, mount, task.blob_path)
                if current_id != task.physical_id:
                    raise RuntimeError("Storage mapping changed after cleanup was staged")
                await self.objects.blobs.deleting(blob)
                await call_adapter(
                    adapter,
                    "delete_object",
                    policy,
                    mount,
                    task.blob_path,
                    hard_delete=True,
                )
                await self.objects.blobs.deleted(blob)
                await self.tasks.done(task)
                completed += 1
            except Exception as exc:  # provider errors stay retryable in the outbox
                await self.objects.blobs.active(blob)
                await self.tasks.retry(task, exc)
        return completed

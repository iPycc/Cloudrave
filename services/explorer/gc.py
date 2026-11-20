"""Transactional object-GC outbox and retry worker."""

from __future__ import annotations

from hashlib import sha256

from drives.ids import provider_id
from models.enums import NodeType
from models.gc import GcTask
from repositories.gc import GcRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from store.call import call_adapter
from store.errors import is_not_found
from store.key import physical_key
from store.manager import StorageManager


PATH_PROVIDERS = {"tencent_cos", "aliyun_oss"}


def _cleanup_identity(extra, size):
    version = str(extra.get("version_id") or "").strip()
    return {
        "size": size,
        "etag": str(extra.get("etag") or "").strip('"') or None,
        "modified": str(extra["last_modified"]) if extra.get("last_modified") else None,
        "version_id": version if version and version.lower() != "null" else None,
        "external": extra.get("origin") == "bucket_import",
    }


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
        nodes = {node.id: node for node in await self.nodes.list_by_ids(node_ids)}
        mount_ids = {node.mount_id for node in nodes.values()}
        mounts = {mount.id: mount for mount in await self.mounts.list_by_ids(list(mount_ids))}
        policy_ids = {record.policy_id for record in records} | {mount.policy_id for mount in mounts.values()}
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
                identity = await call_adapter(adapter, "physical_id", policy, mount, record.blob_path)
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
        protected_ids = {
            identities[record.id]
            for record in records
            if record.id in identities and (record.extra or {}).get("origin") == "bucket_import"
            and (mounts[nodes[record.node_id].mount_id].extra or {}).get("import_delete_source") is not True
        }
        migrated = await self.objects.migrated_targets(set(identities.values()))
        candidates: dict[str, tuple[int, int, str, dict | None]] = {}
        for record in records:
            identity = identities.get(record.id)
            node = nodes.get(record.node_id)
            if not identity or node is None or identity in referenced_ids or identity in protected_ids:
                continue
            legacy_key = (record.policy_id, node.mount_id, record.blob_path)
            if legacy_key in legacy_refs:
                continue
            mount = mounts[node.mount_id]
            policy = policies[record.policy_id]
            extra = record.extra or {}
            guarded = provider_id(policy.provider) in PATH_PROVIDERS and (
                (mount.extra or {}).get("object_key_template") is not None or extra.get("path_blob_id") or identity in migrated
            )
            source = _cleanup_identity(extra, node.size) if guarded else None
            if identity not in candidates or (source is not None and candidates[identity][3] is None):
                candidates[identity] = (policy.id, mount.id, record.blob_path, source)

        task_ids: list[int] = []
        for identity, (policy_id, mount_id, blob_path, source) in candidates.items():
            task = await self.tasks.create(
                policy_id=policy_id,
                mount_id=mount_id,
                blob_path=blob_path,
                physical_id=identity,
                source_identity=source,
            )
            if source is None:
                task_ids.append(task.id)
        for node in nodes.values():
            if node.type != NodeType.FOLDER or not node.blob_path:
                continue
            mount = mounts.get(node.mount_id)
            policy = policies.get(mount.policy_id) if mount else None
            if policy is not None and provider_id(policy.provider) in PATH_PROVIDERS:
                marker = physical_key(policy, mount, node.blob_path) + "/"
                await self.tasks.create(
                    policy_id=policy.id, mount_id=mount.id, blob_path=node.blob_path + "/",
                    physical_id=sha256(marker.encode("utf-8")).hexdigest(),
                    source_identity={"directory_marker": True, "size": 0},
                )
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
            source = task.source_identity
            blob = await self.objects.blobs.lock(task.physical_id)
            if blob.state == "deleted" and source is None:
                await self.tasks.done(task)
                completed += 1
                continue
            try:
                if await self.objects.has_migration_reservation(task.physical_id):
                    # A migration writes its destination after reserving the
                    # same blob lock. Revisit cleanup only after it resolves.
                    await self.tasks.retry(task, "目标对象由路径迁移保留")
                    continue
                if (await self.objects.has_physical_reference(task.physical_id) or
                    await self.objects.retained_migration_source(task.physical_id)):
                    await self.objects.blobs.active(blob)
                    await self.tasks.skipped(task)
                    continue
                mount = await self.mounts.get_by_id(task.mount_id)
                policy = await self.policies.get_by_id(task.policy_id)
                if mount is None or policy is None:
                    raise RuntimeError("Storage policy or mount no longer exists")
                adapter = self.storage_manager.get_adapter(policy)
                current_id = await call_adapter(adapter, "physical_id", policy, mount, task.blob_path)
                if current_id != task.physical_id:
                    raise RuntimeError("Storage mapping changed after cleanup was staged")
                if source is not None:
                    if source.get("external") and (mount.extra or {}).get("import_delete_source") is not True:
                        await self.tasks.skipped(task)
                        continue
                    if not mount.is_enabled or mount.read_only or policy.status != "active":
                        raise RuntimeError("Storage is no longer writable")
                    try:
                        await call_adapter(adapter, "delete_object_version", policy, mount, task.blob_path, source["version_id"])
                    except Exception as exc:
                        if not is_not_found(exc):
                            raise
                    # Other versions at the same key may still be live.
                    await self.objects.blobs.active(blob)
                    await self.tasks.done(task)
                    completed += 1
                    continue
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

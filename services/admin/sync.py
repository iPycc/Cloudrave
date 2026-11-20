"""Mirror an object-storage prefix into Cloudrave's node/object metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import NodeStatus, NodeType
from models.blob import BlobRef
from models.tables import BucketMount, MountSyncConflict, MountSyncTask, Node, StoragePolicy, User
from repositories.audit_logs import AuditLogRepository
from repositories.blobs import BlobUnavailable
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from schemas.sync import MountSyncRead, MountSyncRequest
from services.admin.scan import BucketItem, BucketPage, BucketScanner


@dataclass
class _PageCache:
    root: Node | None = None
    folders: dict[tuple[int, str], Node | None] = field(default_factory=dict)
    revivals: dict[str, tuple[int, bool]] = field(default_factory=dict)
    managed_markers: set[str] = field(default_factory=set)


class MountSyncService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        nodes: NodeRepository,
        objects: ObjectRepository,
        audits: AuditLogRepository,
        scanner: BucketScanner,
    ) -> None:
        self.session = session
        self.mounts = mounts
        self.policies = policies
        self.nodes = nodes
        self.objects = objects
        self.audits = audits
        self.scanner = scanner

    async def status(self, mount_id: int) -> MountSyncRead:
        mount = await self._require_mount(mount_id)
        return self._read(mount, self._state(mount))

    async def run(
        self,
        request: Request,
        actor: User,
        mount_id: int,
        payload: MountSyncRequest,
        *,
        generation: str | None = None,
        lease: tuple[str, str] | None = None,
    ) -> MountSyncRead:
        mount, policy, expected = await self._claim_page(mount_id, payload, generation=generation)

        if expected.get("phase") == "finalizing":
            return await self._finalize_page(request, actor, mount_id, expected, lease=lease)

        try:
            page = await self.scanner.list_page(
                policy,
                mount,
                cursor=expected.get("cursor"),
                page_size=payload.page_size,
            )
            revivals = await self._probe_revivals(policy, mount, page)
        except Exception as exc:
            await self.session.rollback()
            await self._record_error(mount_id, expected, exc, lease=lease)
            raise self._sync_error(exc) from exc

        return await self._apply_page(request, actor, mount_id, expected, policy, page, lease=lease, revivals=revivals)

    @staticmethod
    def _physical_id(mount: BucketMount, item: BucketItem) -> str:
        return item.physical_id or hashlib.sha256(f"{mount.policy_id}\0{item.physical_key}".encode()).hexdigest()

    @staticmethod
    def _path_parts(item: BucketItem) -> list[str] | None:
        if item.key.startswith("/") or "\\" in item.key or "\x00" in item.key:
            return None
        key = item.key[:-1] if item.is_directory and item.key.endswith("/") else item.key
        parts = key.split("/")
        return None if any(not part or part in {".", ".."} or len(part) > 255 for part in parts) else parts

    async def _probe_revivals(self, policy: StoragePolicy, mount: BucketMount, page: BucketPage) -> dict[str, tuple[int, bool]]:
        """Check deleted identities without holding the mount or blob write locks."""
        keys = {self._physical_id(mount, item): item.key for item in page.items
                if not item.is_directory and self._path_parts(item) is not None}
        states: list[tuple[str, int]] = []
        identities = list(keys)
        for offset in range(0, len(identities), 400):
            rows = await self.session.execute(select(BlobRef.physical_id, BlobRef.revision).where(
                BlobRef.physical_id.in_(identities[offset:offset + 400]), BlobRef.state == "deleted",
            ))
            states.extend(rows.tuples())
        # Release the read transaction and its connection before provider I/O.
        await self.session.commit()
        results = {}
        for identity, revision in states:
            results[identity] = (revision, await self.scanner.exists(policy, mount, keys[identity]))
        return results

    async def _claim_page(
        self,
        mount_id: int,
        payload: MountSyncRequest,
        *,
        generation: str | None = None,
    ) -> tuple[BucketMount, StoragePolicy, dict]:
        mount = await self.mounts.get_by_id_for_update(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        await self.session.refresh(mount)
        if not mount.is_enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bucket mount is disabled")
        if mount.mode != "mirror":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Managed mounts are authoritative and cannot import external bucket objects",
            )
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None or policy.status != "active":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        if not self.scanner.storage_manager.get_adapter(policy).capabilities.supports_mirror:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This storage drive does not support bucket mirror synchronization",
            )

        state = self._state(mount)
        if generation is not None and (
            state.get("generation") != generation or state.get("status") != "running"
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Synchronization task is no longer active")
        if payload.restart or state.get("status") != "running":
            state = self._new_state()
            state["mapping"] = self.mapping_signature(policy, mount)
            await self.mounts.save_sync_state(mount, state)
        elif not state.get("mapping"):
            state["mapping"] = self.mapping_signature(policy, mount)
            await self.mounts.save_sync_state(mount, state)

        # Release the row lock before the remote provider request. The generation and
        # cursor below act as an optimistic page token when the response returns.
        await self.session.commit()
        return mount, policy, dict(state)

    async def _apply_page(
        self,
        request: Request,
        actor: User,
        mount_id: int,
        expected: dict,
        policy: StoragePolicy,
        page: BucketPage,
        *,
        lease: tuple[str, str] | None = None,
        revivals: dict[str, tuple[int, bool]] | None = None,
    ) -> MountSyncRead:
        page_error: Exception | None = None
        result: MountSyncRead

        async with self.session.begin():
            mount = await self.mounts.get_by_id_for_update(mount_id)
            if mount is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
            await self._require_lease(mount_id, lease)
            await self.session.refresh(mount)
            current = self._state(mount)
            current_policy = await self.policies.get_by_id(mount.policy_id)
            if current_policy is not None:
                await self.session.refresh(current_policy)

            invalid_reason: str | None = None
            if not mount.is_enabled:
                invalid_reason = "Bucket mount was disabled while the sync page was in flight"
            elif mount.mode != "mirror":
                invalid_reason = "Bucket mount mode changed while the sync page was in flight"
            elif (
                mount.policy_id != policy.id
                or current_policy is None
                or current_policy.status != "active"
                or not self.scanner.storage_manager.get_adapter(current_policy).capabilities.supports_mirror
                or current.get("mapping") != self.mapping_signature(current_policy, mount)
            ):
                invalid_reason = "Storage policy changed while the sync page was in flight"

            if not self._same_page(current, expected):
                result = self._read(mount, current)
            elif invalid_reason is not None:
                failed = dict(current)
                failed["status"] = "error"
                failed["error"] = invalid_reason
                await self.mounts.save_sync_state(mount, failed)
                result = self._read(mount, failed)
            else:
                next_state = dict(current)
                cache = _PageCache(revivals=revivals or {})
                try:
                    async with self.session.begin_nested():
                        cache.managed_markers = await self.nodes.directory_marker_keys(
                            mount.id, {item.key.removesuffix("/") for item in page.items if item.is_directory},
                        )
                        conflicts: dict[str, tuple[str, str]] = {}
                        for item in page.items:
                            next_state["scanned"] += 1
                            outcome, reason = await self._upsert_item(
                                mount,
                                item,
                                next_state["generation"],
                                cache,
                            )
                            if outcome == "conflicts":
                                encoded = json.dumps(item.key, ensure_ascii=True)
                                identity = hashlib.sha256(encoded.encode("ascii")).hexdigest()
                                conflicts[identity] = (encoded, reason or "name_conflict")
                            else:
                                next_state[outcome] = next_state.get(outcome, 0) + 1

                        if conflicts:
                            existing: set[str] = set()
                            identities = list(conflicts)
                            for offset in range(0, len(identities), 400):
                                existing.update(await self.session.scalars(
                                    select(MountSyncConflict.key_hash).where(
                                        MountSyncConflict.mount_id == mount.id,
                                        MountSyncConflict.generation == next_state["generation"],
                                        MountSyncConflict.key_hash.in_(identities[offset:offset + 400]),
                                    )
                                ))
                            for identity, (encoded, reason) in conflicts.items():
                                if identity not in existing:
                                    self.session.add(MountSyncConflict(
                                        mount_id=mount.id, generation=next_state["generation"],
                                        key_hash=identity, key_json=encoded, reason=reason,
                                    ))
                                    next_state["conflicts"] = next_state.get("conflicts", 0) + 1

                        next_state["cursor"] = page.next_cursor
                        if page.complete:
                            next_state["cursor"] = None
                            next_state["phase"] = "finalizing"
                            next_state["finalize_after_id"] = 0
                except Exception as exc:
                    page_error = exc
                    failed = dict(current)
                    failed["status"] = "error"
                    failed["error"] = str(exc)[:1000]
                    await self.mounts.save_sync_state(mount, failed)
                    result = self._read(mount, failed)
                else:
                    next_state["error"] = None
                    next_state["retry_count"] = 0
                    await self.mounts.save_sync_state(mount, next_state)
                    await self._audit(request, actor, mount, next_state)
                    result = self._read(mount, next_state)

        if page_error is not None:
            raise self._sync_error(page_error) from page_error
        return result

    async def _record_error(
        self, mount_id: int, expected: dict, exc: Exception, *, lease: tuple[str, str] | None = None,
    ) -> None:
        async with self.session.begin():
            mount = await self.mounts.get_by_id_for_update(mount_id)
            if mount is None:
                return
            await self._require_lease(mount_id, lease)
            await self.session.refresh(mount)
            current = self._state(mount)
            if not self._same_page(current, expected):
                return
            current["status"] = "error"
            current["error"] = str(exc)[:1000]
            await self.mounts.save_sync_state(mount, current)

    async def _audit(self, request: Request, actor: User, mount: BucketMount, state: dict) -> None:
        await self.audits.create(
            actor_id=actor.id,
            action="mount.sync",
            target=f"mount:{mount.id}",
            detail=f"status={state['status']},scanned={state['scanned']},cursor={state.get('cursor') or '-'}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

    async def _upsert_item(
        self,
        mount: BucketMount,
        item: BucketItem,
        generation: str,
        cache: _PageCache,
    ) -> tuple[str, str | None]:
        parts = self._path_parts(item)
        if parts is None:
            return "conflicts", "invalid_path"
        key = item.key[:-1] if item.is_directory and item.key.endswith("/") else item.key

        if item.is_directory:
            marker_hash = hashlib.sha256((item.physical_key.rstrip("/") + "/").encode("utf-8")).hexdigest()
            if key in cache.managed_markers or await self.objects.has_migration_marker(mount.id, marker_hash):
                return "skipped", None
            if cache.root is None:
                cache.root = await self.nodes.ensure_mount_root(
                    owner_id=mount.owner_id, mount_id=mount.id, name=mount.name,
                )
            parent_id = cache.root.id
            created = False
            for name in parts:
                existing = await self.nodes.get_child_any(mount.id, parent_id, name)
                try:
                    folder = await self.nodes.ensure_folder(
                        owner_id=mount.owner_id, mount_id=mount.id, parent_id=parent_id, name=name,
                    )
                except (IntegrityError, ValueError):
                    return "conflicts", "folder_conflict"
                created = created or existing is None
                parent_id = folder.id
            return ("created" if created else "unchanged"), None

        physical_id = self._physical_id(mount, item)
        references = await self.objects.list_physical_in_mount(mount.id, mount.policy_id, physical_id, key)
        for record in references:
            if (record.extra or {}).get("origin") == "bucket_import":
                record.extra = {**record.extra, "sync_generation": generation}
        if any((record.extra or {}).get("origin") != "bucket_import" for record in references):
            return "skipped", None
        try:
            await self.objects.blobs.ensure(physical_id)
        except BlobUnavailable:
            # The blob lock is held here. Accept only the exact deleted state
            # checked outside the transaction; a concurrent GC invalidates it.
            probe = cache.revivals.get(physical_id)
            blob = await self.objects.blobs.lock(physical_id)
            if probe is None or not probe[1] or blob.state != "deleted" or blob.revision != probe[0]:
                return "conflicts", "source_unavailable"
            await self.objects.blobs.ensure(physical_id, revive=True)

        if await self.objects.has_migration_reservation(physical_id):
            return "skipped", None
        retained = await self.objects.retained_migration_source(physical_id, mount.id)
        if retained is not None:
            identity = json.loads(retained.source_identity_json)
            if item.etag == identity["etag"] and item.size == identity["size"]:
                return "skipped", None

        if cache.root is None:
            cache.root = await self.nodes.ensure_mount_root(
                owner_id=mount.owner_id,
                mount_id=mount.id,
                name=mount.name,
            )
        parent_id = cache.root.id
        for folder_name in parts[:-1]:
            cache_key = (parent_id, folder_name)
            if cache_key in cache.folders:
                folder = cache.folders[cache_key]
            else:
                try:
                    folder = await self.nodes.ensure_folder(
                        owner_id=mount.owner_id,
                        mount_id=mount.id,
                        parent_id=parent_id,
                        name=folder_name,
                    )
                except (IntegrityError, ValueError):
                    return "conflicts", "folder_conflict"
                cache.folders[cache_key] = folder
            if folder is None or folder.type != NodeType.FOLDER or folder.deleted_at is not None:
                return "conflicts", "folder_conflict"
            parent_id = folder.id

        file_name = parts[-1]
        node = await self.nodes.get_child_any(mount.id, parent_id, file_name)
        if references and all(node is None or record.node_id != node.id for record in references):
            for record in references:
                extra = dict(record.extra or {})
                if (extra.get("etag") != item.etag or extra.get("last_modified") != item.last_modified
                        or not (item.etag or item.last_modified)):
                    record.checksum = None
                extra["sync_generation"] = generation
                record.extra = extra
            return "conflicts", "indexed_elsewhere"
        if node is None:
            node = await self.nodes.create_file(
                owner_id=mount.owner_id,
                mount_id=mount.id,
                parent_id=parent_id,
                name=file_name,
                blob_path=key,
                size=item.size,
            )
            await self.objects.create_or_update(
                node_id=node.id,
                policy_id=mount.policy_id,
                blob_path=key,
                physical_id=physical_id,
                extra=self._object_extra(item, generation),
            )
            return "created", None
        if node.type != NodeType.FILE:
            return "conflicts", "folder_conflict"

        record = await self.objects.get_by_node_id(node.id)
        if record is None or (record.extra or {}).get("origin") != "bucket_import":
            return "conflicts", "name_conflict"
        if (record.physical_id and record.physical_id != physical_id) or (
            not record.physical_id and record.blob_path != key
        ):
            # A virtual rename or move can leave a different imported object
            # at this name. Never replace its identity during a rescan.
            return "conflicts", "name_conflict"

        old_extra = dict(record.extra or {})
        content_changed = (
            node.size != item.size
            or node.blob_path != key
            or old_extra.get("etag") != item.etag
            or old_extra.get("last_modified") != item.last_modified
        )
        changed = content_changed or bool(old_extra.get("external_missing"))
        in_recycle_bin = node.deleted_at is not None and (
            not old_extra.get("external_missing") or old_extra.get("missing_while_recycled") is True
        )
        if in_recycle_bin and content_changed:
            node.blob_path = key
            node.size = item.size
            node.updated_at = datetime.now(UTC)
        elif not in_recycle_bin and (changed or node.status != NodeStatus.ACTIVE or node.deleted_at is not None):
            await self.nodes.reactivate_file(node, blob_path=key, size=item.size)
        # An imported object's checksum describes its previous contents. If the
        # provider cannot supply a version signal, it cannot be safely reused.
        checksum = record.checksum
        if changed or not (item.etag or item.last_modified):
            checksum = None
        await self.objects.create_or_update(
            node_id=node.id,
            policy_id=mount.policy_id,
            blob_path=key,
            physical_id=physical_id,
            checksum=checksum,
            content_type=record.content_type,
            extra=self._object_extra(item, generation),
        )
        return ("updated" if changed else "unchanged"), None

    async def _finalize_page(
        self, request: Request, actor: User, mount_id: int, expected: dict,
        *, lease: tuple[str, str] | None = None,
    ) -> MountSyncRead:
        async with self.session.begin():
            mount = await self.mounts.get_by_id_for_update(mount_id)
            if mount is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
            await self._require_lease(mount_id, lease)
            current = self._state(mount)
            if not self._same_page(current, expected):
                return self._read(mount, current)
            policy = await self.policies.get_by_id(mount.policy_id)
            if policy is not None:
                await self.session.refresh(policy)
            if (
                not mount.is_enabled or mount.mode != "mirror" or policy is None or policy.status != "active"
                or current.get("mapping") != self.mapping_signature(policy, mount)
            ):
                current["status"] = "error"
                current["error"] = "Storage mount changed during synchronization"
                await self.mounts.save_sync_state(mount, current)
                return self._read(mount, current)

            records = await self.objects.list_for_mount_page(
                mount.id, after_id=int(current.get("finalize_after_id", 0)), limit=500,
            )
            now = datetime.now(UTC)
            started_at = datetime.fromisoformat(current["started_at"])
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            for record, node in records:
                current["finalize_after_id"] = record.id
                extra = dict(record.extra or {})
                if extra.get("origin") != "bucket_import":
                    continue
                if extra.get("sync_generation") == current["generation"]:
                    current["synced_objects"] += 1
                    continue
                updated = record.updated_at
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                node_updated = node.updated_at
                if node_updated.tzinfo is None:
                    node_updated = node_updated.replace(tzinfo=UTC)
                if updated > started_at or node_updated > started_at:
                    continue
                if not extra.get("external_missing"):
                    current["missing"] += 1
                    extra["missing_while_recycled"] = node.deleted_at is not None
                elif node.deleted_at is None:
                    extra["missing_while_recycled"] = False
                extra["external_missing"] = True
                extra["missing_at"] = now.isoformat()
                record.extra = extra
                if not extra.get("missing_while_recycled") or node.deleted_at is None:
                    node.status = NodeStatus.DELETED
                    node.deleted_at = now
            if len(records) < 500:
                current["status"] = "complete"
                current["completed_at"] = _now()
                current["phase"] = "complete"
            current["retry_count"] = 0
            await self.mounts.save_sync_state(mount, current)
            await self._audit(request, actor, mount, current)
            return self._read(mount, current)

    async def _require_lease(self, mount_id: int, lease: tuple[str, str] | None) -> None:
        if lease is None:
            return
        task_id, token = lease
        task = await self.session.scalar(
            select(MountSyncTask.id).where(
                MountSyncTask.id == task_id,
                MountSyncTask.mount_id == mount_id,
                MountSyncTask.status == "running",
                MountSyncTask.lease_token == token,
                MountSyncTask.lease_expires_at > datetime.now(UTC),
            ).with_for_update()
        )
        if task is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Synchronization task lease expired")

    async def _require_mount(self, mount_id: int) -> BucketMount:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        return mount

    @staticmethod
    def _same_page(current: dict, expected: dict) -> bool:
        return (
            current.get("status") == "running"
            and current.get("generation") == expected.get("generation")
            and current.get("cursor") == expected.get("cursor")
            and current.get("phase") == expected.get("phase")
            and current.get("finalize_after_id") == expected.get("finalize_after_id")
        )

    @staticmethod
    def _sync_error(exc: Exception) -> HTTPException:
        message = str(exc)[:1000]
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bucket synchronization failed: {message}",
        )

    @staticmethod
    def _object_extra(item: BucketItem, generation: str) -> dict:
        return {
            "origin": "bucket_import",
            "physical_key": item.physical_key,
            "etag": item.etag,
            "last_modified": item.last_modified,
            "storage_class": item.storage_class,
            "sync_generation": generation,
            "external_missing": False,
        }

    @staticmethod
    def mapping_signature(policy: StoragePolicy, mount: BucketMount) -> str:
        values = [
            policy.id, policy.provider, policy.bucket_name, policy.region, policy.endpoint, policy.base_prefix,
            mount.root_path, (mount.extra or {}).get("legacy_prefixed_keys", False),
            (mount.extra or {}).get("storage_root"), (policy.extra or {}).get("root_id"),
            (mount.extra or {}).get("root_id"),
        ]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False, default=str).encode()).hexdigest()

    @staticmethod
    def _new_state() -> dict:
        return {
            "status": "running",
            "generation": uuid4().hex,
            "cursor": None,
            "phase": "scanning",
            "started_at": _now(),
            "completed_at": None,
            "scanned": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "conflicts": 0,
            "skipped": 0,
            "missing": 0,
            "synced_objects": 0,
            "error": None,
        }

    @staticmethod
    def _state(mount: BucketMount) -> dict:
        state = dict(((mount.extra or {}).get("_sync") or {}))
        if not state:
            state = {
                "status": "never",
                "generation": None,
                "cursor": None,
                "started_at": None,
                "completed_at": None,
                "scanned": 0,
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "conflicts": 0,
                "skipped": 0,
                "missing": 0,
                "synced_objects": 0,
                "error": None,
            }
        return state

    @staticmethod
    def _read(mount: BucketMount, state: dict) -> MountSyncRead:
        return MountSyncRead.model_validate(
            {
                "mount_id": mount.id,
                "mode": mount.mode,
                **state,
                "complete": state.get("status") == "complete",
            }
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()

"""Mirror a COS bucket prefix into Cloudrave's node/object metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from uuid import uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import NodeStatus, NodeType, StoragePolicyType
from models.tables import BucketMount, Node, StoragePolicy, User
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
    policy: StoragePolicy | None = None
    root: Node | None = None
    folders: dict[tuple[int, str], Node | None] = field(default_factory=dict)


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
    ) -> MountSyncRead:
        mount, policy, expected = await self._claim_page(mount_id, payload)

        try:
            page = await self.scanner.list_page(
                policy,
                mount,
                cursor=expected.get("cursor"),
                page_size=payload.page_size,
            )
        except Exception as exc:
            await self._record_error(mount_id, expected, exc)
            raise self._sync_error(exc) from exc

        return await self._apply_page(request, actor, mount_id, expected, policy, page)

    async def _claim_page(
        self,
        mount_id: int,
        payload: MountSyncRequest,
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
        if policy.provider != StoragePolicyType.TENCENT_COS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Bucket mirror synchronization currently requires a Tencent COS policy",
            )

        state = self._state(mount)
        if payload.restart or state.get("status") != "running":
            state = self._new_state()
            await self.mounts.save_sync_state(mount, state)

        # Release the row lock before the remote COS request. The generation and
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
    ) -> MountSyncRead:
        page_error: Exception | None = None
        result: MountSyncRead

        async with self.session.begin():
            mount = await self.mounts.get_by_id_for_update(mount_id)
            if mount is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
            await self.session.refresh(mount)
            current = self._state(mount)
            current_policy = await self.policies.get_by_id(mount.policy_id)

            invalid_reason: str | None = None
            if not mount.is_enabled:
                invalid_reason = "Bucket mount was disabled while the sync page was in flight"
            elif mount.mode != "mirror":
                invalid_reason = "Bucket mount mode changed while the sync page was in flight"
            elif (
                mount.policy_id != policy.id
                or current_policy is None
                or current_policy.status != "active"
                or current_policy.provider != StoragePolicyType.TENCENT_COS
            ):
                invalid_reason = "Storage policy changed while the sync page was in flight"

            if invalid_reason is not None:
                failed = dict(current)
                failed["status"] = "error"
                failed["error"] = invalid_reason
                await self.mounts.save_sync_state(mount, failed)
                result = self._read(mount, failed)
            elif not self._same_page(current, expected):
                # Another request already applied this cursor (or restarted the
                # generation). Discard the stale COS response without replaying it.
                result = self._read(mount, current)
            else:
                next_state = dict(current)
                cache = _PageCache(policy=policy)
                try:
                    async with self.session.begin_nested():
                        for item in page.items:
                            next_state["scanned"] += 1
                            outcome = await self._upsert_item(
                                mount,
                                item,
                                next_state["generation"],
                                cache,
                            )
                            next_state[outcome] += 1

                        next_state["cursor"] = page.next_cursor
                        if page.complete:
                            missing, synced = await self._finish_generation(
                                mount,
                                next_state["generation"],
                            )
                            next_state["missing"] = missing
                            next_state["synced_objects"] = synced
                            next_state["status"] = "complete"
                            next_state["completed_at"] = _now()
                            next_state["cursor"] = None
                except Exception as exc:
                    page_error = exc
                    failed = dict(current)
                    failed["status"] = "error"
                    failed["error"] = str(exc)[:1000]
                    await self.mounts.save_sync_state(mount, failed)
                    result = self._read(mount, failed)
                else:
                    next_state["error"] = None
                    await self.mounts.save_sync_state(mount, next_state)
                    await self._audit(request, actor, mount, next_state)
                    result = self._read(mount, next_state)

        if page_error is not None:
            raise self._sync_error(page_error) from page_error
        return result

    async def _record_error(self, mount_id: int, expected: dict, exc: Exception) -> None:
        async with self.session.begin():
            mount = await self.mounts.get_by_id_for_update(mount_id)
            if mount is None:
                return
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
    ) -> str:
        key = item.key.strip("/")
        if not key:
            return "conflicts"
        parts = key.split("/")
        if any(not part or part in {".", ".."} or len(part) > 255 for part in parts):
            return "conflicts"

        physical_id = item.physical_id or hashlib.sha256(
            f"{mount.policy_id}\0{item.physical_key}".encode()
        ).hexdigest()
        try:
            await self.objects.blobs.ensure(physical_id)
        except BlobUnavailable:
            if cache.policy is None or not await self.scanner.exists(cache.policy, mount, key):
                return "conflicts"
            await self.objects.blobs.ensure(physical_id, revive=True)

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
                    return "conflicts"
                cache.folders[cache_key] = folder
            if folder is None or folder.type != NodeType.FOLDER or folder.deleted_at is not None:
                return "conflicts"
            parent_id = folder.id

        file_name = parts[-1]
        node = await self.nodes.get_child_any(mount.id, parent_id, file_name)
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
            return "created"
        if node.type != NodeType.FILE:
            return "conflicts"

        record = await self.objects.get_by_node_id(node.id)
        if record is None or (record.extra or {}).get("origin") != "bucket_import":
            return "conflicts"

        old_extra = dict(record.extra or {})
        changed = (
            node.size != item.size
            or node.blob_path != key
            or old_extra.get("etag") != item.etag
            or old_extra.get("last_modified") != item.last_modified
            or bool(old_extra.get("external_missing"))
        )
        await self.nodes.reactivate_file(node, blob_path=key, size=item.size)
        await self.objects.create_or_update(
            node_id=node.id,
            policy_id=mount.policy_id,
            blob_path=key,
            physical_id=physical_id,
            checksum=record.checksum,
            content_type=record.content_type,
            extra=self._object_extra(item, generation),
        )
        return "updated" if changed else "unchanged"

    async def _finish_generation(self, mount: BucketMount, generation: str) -> tuple[int, int]:
        missing = 0
        synced = 0
        now = datetime.now(UTC)
        after_id = 0
        while True:
            records = await self.objects.list_for_mount_page(mount.id, after_id=after_id, limit=500)
            if not records:
                break
            for record, node in records:
                after_id = record.id
                extra = dict(record.extra or {})
                if extra.get("origin") != "bucket_import":
                    continue
                if extra.get("sync_generation") == generation:
                    synced += 1
                    continue
                if not extra.get("external_missing"):
                    missing += 1
                extra["external_missing"] = True
                extra["missing_at"] = now.isoformat()
                record.extra = extra
                node.status = NodeStatus.DELETED
                node.deleted_at = now
            await self.session.flush()
        return missing, synced

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
    def _new_state() -> dict:
        return {
            "status": "running",
            "generation": uuid4().hex,
            "cursor": None,
            "started_at": _now(),
            "completed_at": None,
            "scanned": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "conflicts": 0,
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

"""Bucket mount persistence operations."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import UploadState
from models.tables import BucketMount, UploadSessionRecord
from models.gc import GcTask
from schemas.mount import BucketMountCreate, BucketMountUpdate


class BucketMountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[BucketMount]:
        result = await self.session.scalars(select(BucketMount).order_by(BucketMount.id.asc()))
        return list(result)

    async def list_for_owner(self, owner_id: int) -> list[BucketMount]:
        result = await self.session.scalars(
            select(BucketMount).where(BucketMount.owner_id == owner_id).order_by(BucketMount.id.asc())
        )
        return list(result)

    async def get_by_id(self, mount_id: int) -> BucketMount | None:
        return await self.session.get(BucketMount, mount_id)

    async def get_by_id_for_update(self, mount_id: int) -> BucketMount | None:
        return await self.session.scalar(
            select(BucketMount).where(BucketMount.id == mount_id).with_for_update()
        )

    async def list_by_ids(self, mount_ids: list[int]) -> list[BucketMount]:
        if not mount_ids:
            return []
        result = await self.session.scalars(select(BucketMount).where(BucketMount.id.in_(mount_ids)))
        return list(result)

    async def get_by_owner_and_slug(self, owner_id: int, mount_slug: str) -> BucketMount | None:
        statement = select(BucketMount).where(
            BucketMount.owner_id == owner_id,
            BucketMount.mount_slug == mount_slug,
        )
        return await self.session.scalar(statement)

    async def has_pending_gc(self, mount_id: int) -> bool:
        return bool(await self.pending_gc_count(mount_id))

    async def pending_gc_count(self, mount_id: int) -> int:
        count = await self.session.scalar(
            select(func.count(GcTask.id)).where(
                GcTask.mount_id == mount_id,
                GcTask.state.in_(("pending", "processing")),
            )
        )
        return int(count or 0)

    async def has_active_uploads(self, mount_id: int) -> bool:
        return bool(await self.active_upload_count(mount_id))

    async def active_upload_count(self, mount_id: int) -> int:
        terminal = (UploadState.COMPLETED, UploadState.ABORTED, UploadState.EXPIRED)
        count = await self.session.scalar(
            select(func.count(UploadSessionRecord.id)).where(
                UploadSessionRecord.mount_id == mount_id,
                UploadSessionRecord.state.not_in(terminal),
            )
        )
        return int(count or 0)

    async def create(self, owner_id: int, payload: BucketMountCreate) -> BucketMount:
        values = payload.model_dump(exclude={"owner_id", "mode", "read_only", "legacy_prefixed_keys"})
        extra = dict(values.get("extra") or {})
        extra["mode"] = payload.mode
        extra["read_only"] = payload.read_only
        extra["legacy_prefixed_keys"] = payload.legacy_prefixed_keys
        values["extra"] = extra
        mount = BucketMount(owner_id=owner_id, **values)
        self.session.add(mount)
        await self.session.flush()
        return mount

    async def create_managed_local_mount(
        self,
        *,
        owner_id: int,
        policy_id: int,
        name: str,
        mount_slug: str,
        provider_label: str,
        root_path: str = "",
        extra: dict | None = None,
    ) -> BucketMount:
        mount = BucketMount(
            owner_id=owner_id,
            policy_id=policy_id,
            name=name,
            mount_slug=mount_slug,
            root_path=root_path.strip().strip("/"),
            provider_label=provider_label,
            is_enabled=True,
            extra=dict(extra or {}),
        )
        self.session.add(mount)
        await self.session.flush()
        return mount

    async def create_guest_workspace(
        self,
        *,
        owner_id: int,
        policy_id: int,
        source_mount_id: int,
        uid: str,
        root_path: str,
        quota_bytes: int,
        expires_at: str | None,
    ) -> BucketMount:
        mount = BucketMount(
            owner_id=owner_id,
            policy_id=policy_id,
            name="Guest Storage",
            mount_slug=uid.lower(),
            root_path=root_path.strip().strip("/"),
            provider_label="Guest Storage",
            is_enabled=True,
            quota_bytes=quota_bytes,
            extra={
                "mode": "managed",
                "read_only": False,
                "system_managed": True,
                "guest_workspace": True,
                "guest_source_mount_id": source_mount_id,
                "guest_expires_at": expires_at,
            },
        )
        self.session.add(mount)
        await self.session.flush()
        return mount

    async def update(self, mount: BucketMount, payload: BucketMountUpdate) -> BucketMount:
        values = payload.model_dump(exclude_unset=True)
        mode = values.pop("mode", None)
        read_only = values.pop("read_only", None)
        legacy_prefixed_keys = values.pop("legacy_prefixed_keys", None)
        reset_sync = (
            ("root_path" in values and values["root_path"] != mount.root_path)
            or (mode is not None and mode != mount.mode)
        )
        if "extra" in values:
            replacement = dict(values["extra"] or {})
            current_extra = mount.extra or {}
            for reserved_key in ("_sync", "mode", "read_only", "legacy_prefixed_keys"):
                if reserved_key in current_extra:
                    replacement[reserved_key] = current_extra[reserved_key]
            values["extra"] = replacement
        for key, value in values.items():
            setattr(mount, key, value)
        if mode is not None or read_only is not None or legacy_prefixed_keys is not None:
            extra = dict(mount.extra or {})
            if mode is not None:
                extra["mode"] = mode
            if read_only is not None:
                extra["read_only"] = read_only
            if legacy_prefixed_keys is not None:
                extra["legacy_prefixed_keys"] = legacy_prefixed_keys
            mount.extra = extra
        if reset_sync:
            extra = dict(mount.extra or {})
            extra.pop("_sync", None)
            mount.extra = extra
        await self.session.flush()
        return mount

    async def save_sync_state(self, mount: BucketMount, state: dict) -> None:
        extra = dict(mount.extra or {})
        extra["_sync"] = dict(state)
        mount.extra = extra
        await self.session.flush()

    async def delete(self, mount: BucketMount) -> None:
        await self.session.delete(mount)
        await self.session.flush()

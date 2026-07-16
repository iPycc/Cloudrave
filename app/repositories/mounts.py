"""Bucket mount persistence operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import BucketMount
from app.schemas.mount import BucketMountCreate, BucketMountUpdate


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

    async def get_by_owner_and_slug(self, owner_id: int, mount_slug: str) -> BucketMount | None:
        statement = select(BucketMount).where(
            BucketMount.owner_id == owner_id,
            BucketMount.mount_slug == mount_slug,
        )
        return await self.session.scalar(statement)

    async def create(self, owner_id: int, payload: BucketMountCreate) -> BucketMount:
        values = payload.model_dump(exclude={"owner_id"})
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

    async def update(self, mount: BucketMount, payload: BucketMountUpdate) -> BucketMount:
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(mount, key, value)
        await self.session.flush()
        return mount

    async def delete(self, mount: BucketMount) -> None:
        await self.session.delete(mount)
        await self.session.flush()

"""Storage policy persistence operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import StoragePolicy
from app.schemas.policy import StoragePolicyCreate, StoragePolicyUpdate


class StoragePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[StoragePolicy]:
        result = await self.session.scalars(select(StoragePolicy).order_by(StoragePolicy.id.asc()))
        return list(result)

    async def get_by_id(self, policy_id: int) -> StoragePolicy | None:
        return await self.session.get(StoragePolicy, policy_id)

    async def get_by_name(self, name: str) -> StoragePolicy | None:
        statement = select(StoragePolicy).where(StoragePolicy.name == name)
        return await self.session.scalar(statement)

    async def create(self, payload: StoragePolicyCreate) -> StoragePolicy:
        policy = StoragePolicy(**payload.model_dump())
        self.session.add(policy)
        await self.session.flush()
        return policy

    async def update(self, policy: StoragePolicy, payload: StoragePolicyUpdate) -> StoragePolicy:
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(policy, key, value)
        await self.session.flush()
        return policy

    async def delete(self, policy: StoragePolicy) -> None:
        await self.session.delete(policy)
        await self.session.flush()

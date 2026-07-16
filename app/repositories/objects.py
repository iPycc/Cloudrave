"""Object metadata persistence operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import ObjectRecord


class ObjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_node_id(self, node_id: int) -> ObjectRecord | None:
        statement = select(ObjectRecord).where(ObjectRecord.node_id == node_id)
        return await self.session.scalar(statement)

    async def create_or_update(
        self,
        *,
        node_id: int,
        policy_id: int,
        blob_path: str,
        checksum: str | None = None,
        content_type: str | None = None,
        extra: dict | None = None,
    ) -> ObjectRecord:
        record = await self.get_by_node_id(node_id)
        if record is None:
            record = ObjectRecord(
                node_id=node_id,
                policy_id=policy_id,
                blob_path=blob_path,
                checksum=checksum,
                content_type=content_type,
                extra=extra or {},
            )
            self.session.add(record)
        else:
            record.policy_id = policy_id
            record.blob_path = blob_path
            record.checksum = checksum
            record.content_type = content_type
            record.extra = extra or {}
        await self.session.flush()
        return record

    async def delete_by_node_ids(self, node_ids: list[int]) -> None:
        result = await self.session.scalars(select(ObjectRecord).where(ObjectRecord.node_id.in_(node_ids)))
        for record in result:
            await self.session.delete(record)
        await self.session.flush()

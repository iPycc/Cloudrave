"""Object metadata persistence operations."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Node, ObjectRecord
from app.repositories.blobs import BlobRepository


class ObjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.blobs = BlobRepository(session)

    async def get_by_node_id(self, node_id: int) -> ObjectRecord | None:
        statement = select(ObjectRecord).where(ObjectRecord.node_id == node_id)
        return await self.session.scalar(statement)

    async def get_by_node_ids(self, node_ids: list[int]) -> list[ObjectRecord]:
        if not node_ids:
            return []
        result = await self.session.scalars(select(ObjectRecord).where(ObjectRecord.node_id.in_(node_ids)))
        return list(result)

    async def get_by_checksum(
        self,
        checksum: str,
        *,
        policy_id: int | None = None,
        mount_id: int | None = None,
    ) -> ObjectRecord | None:
        statement = select(ObjectRecord).join(Node, Node.id == ObjectRecord.node_id).where(
            ObjectRecord.checksum == checksum,
            Node.deleted_at.is_(None),
        )
        if policy_id is not None:
            statement = statement.where(ObjectRecord.policy_id == policy_id)
        if mount_id is not None:
            statement = statement.where(Node.mount_id == mount_id)
        return await self.session.scalar(statement)

    async def referenced_keys(
        self,
        candidates: list[ObjectRecord],
        *,
        excluding_node_ids: list[int],
    ) -> set[tuple[int, int, str]]:
        """Return physical keys that still have a virtual reference outside a deletion set."""

        if not candidates:
            return set()
        excluded = set(excluding_node_ids)
        candidate_nodes = {
            record.node_id: record
            for record in candidates
        }
        nodes = await self.session.scalars(select(Node).where(Node.id.in_(candidate_nodes)))
        keys_by_mount: dict[tuple[int, int], set[str]] = {}
        for node in nodes:
            record = candidate_nodes[node.id]
            keys_by_mount.setdefault((record.policy_id, node.mount_id), set()).add(record.blob_path)

        referenced: set[tuple[int, int, str]] = set()
        for (policy_id, mount_id), blob_paths in keys_by_mount.items():
            statement = (
                select(ObjectRecord.blob_path)
                .join(Node, Node.id == ObjectRecord.node_id)
                .where(
                    ObjectRecord.policy_id == policy_id,
                    ObjectRecord.blob_path.in_(blob_paths),
                    Node.mount_id == mount_id,
                )
                .distinct()
            )
            if excluded:
                statement = statement.where(ObjectRecord.node_id.not_in(excluded))
            rows = await self.session.scalars(statement)
            referenced.update((policy_id, mount_id, blob_path) for blob_path in rows)
        return referenced

    async def create_or_update(
        self,
        *,
        node_id: int,
        policy_id: int,
        blob_path: str,
        physical_id: str | None = None,
        revive_physical: bool = False,
        checksum: str | None = None,
        content_type: str | None = None,
        extra: dict | None = None,
    ) -> ObjectRecord:
        if physical_id:
            await self.blobs.ensure(physical_id, revive=revive_physical)
        record = await self.get_by_node_id(node_id)
        if record is None:
            record = ObjectRecord(
                node_id=node_id,
                policy_id=policy_id,
                blob_path=blob_path,
                physical_id=physical_id,
                checksum=checksum,
                content_type=content_type,
                extra=extra or {},
            )
            self.session.add(record)
        else:
            record.policy_id = policy_id
            record.blob_path = blob_path
            record.physical_id = physical_id
            record.checksum = checksum
            record.content_type = content_type
            record.extra = extra or {}
        await self.session.flush()
        return record

    async def count_physical_references(
        self,
        *,
        physical_id: str | None,
        policy_id: int,
        mount_id: int,
        blob_path: str,
    ) -> int:
        statement = select(func.count(ObjectRecord.id)).join(Node, Node.id == ObjectRecord.node_id)
        if physical_id:
            statement = statement.where(
                or_(
                    ObjectRecord.physical_id == physical_id,
                    (
                        ObjectRecord.physical_id.is_(None)
                        & (ObjectRecord.policy_id == policy_id)
                        & (Node.mount_id == mount_id)
                        & (ObjectRecord.blob_path == blob_path)
                    ),
                )
            )
        else:
            statement = statement.where(
                ObjectRecord.policy_id == policy_id,
                Node.mount_id == mount_id,
                ObjectRecord.blob_path == blob_path,
            )
        return int(await self.session.scalar(statement) or 0)

    async def referenced_physical(
        self,
        physical_ids: set[str],
        *,
        excluding_node_ids: list[int],
    ) -> set[str]:
        if not physical_ids:
            return set()
        statement = select(ObjectRecord.physical_id).where(ObjectRecord.physical_id.in_(physical_ids)).distinct()
        if excluding_node_ids:
            statement = statement.where(ObjectRecord.node_id.not_in(excluding_node_ids))
        rows = await self.session.scalars(statement)
        return {value for value in rows if value}

    async def has_physical_reference(self, physical_id: str) -> bool:
        statement = select(ObjectRecord.id).where(ObjectRecord.physical_id == physical_id).limit(1)
        return await self.session.scalar(statement) is not None

    async def list_missing_physical_page(
        self,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[tuple[ObjectRecord, Node]]:
        rows = await self.session.execute(
            select(ObjectRecord, Node)
            .join(Node, Node.id == ObjectRecord.node_id)
            .where(
                or_(ObjectRecord.physical_id.is_(None), func.length(ObjectRecord.physical_id) != 64),
                ObjectRecord.id > after_id,
            )
            .order_by(ObjectRecord.id.asc())
            .limit(limit)
        )
        return list(rows.tuples())

    async def save_physical(self, record: ObjectRecord, physical_id: str) -> None:
        await self.blobs.ensure(physical_id)
        record.physical_id = physical_id
        await self.session.flush()

    async def delete_by_node_ids(self, node_ids: list[int]) -> None:
        result = await self.session.scalars(select(ObjectRecord).where(ObjectRecord.node_id.in_(node_ids)))
        for record in result:
            await self.session.delete(record)
        await self.session.flush()

    async def list_for_mount(self, mount_id: int) -> list[tuple[ObjectRecord, Node]]:
        rows = await self.session.execute(
            select(ObjectRecord, Node)
            .join(Node, Node.id == ObjectRecord.node_id)
            .where(Node.mount_id == mount_id)
            .order_by(ObjectRecord.id.asc())
        )
        return list(rows.tuples())

    async def list_for_mount_page(
        self,
        mount_id: int,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[tuple[ObjectRecord, Node]]:
        rows = await self.session.execute(
            select(ObjectRecord, Node)
            .join(Node, Node.id == ObjectRecord.node_id)
            .where(Node.mount_id == mount_id, ObjectRecord.id > after_id)
            .order_by(ObjectRecord.id.asc())
            .limit(limit)
        )
        return list(rows.tuples())

    async def save_extra(self, record: ObjectRecord, extra: dict) -> None:
        record.extra = dict(extra)
        await self.session.flush()

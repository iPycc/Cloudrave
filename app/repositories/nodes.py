"""Virtual file node persistence operations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NodeStatus, NodeType
from app.models.tables import Node


class NodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, node_id: int) -> Node | None:
        return await self.session.get(Node, node_id)

    async def list_by_ids(self, node_ids: list[int]) -> list[Node]:
        if not node_ids:
            return []
        result = await self.session.scalars(select(Node).where(Node.id.in_(node_ids)).order_by(Node.id.asc()))
        return list(result)

    async def list_children(self, mount_id: int, parent_id: int | None, *, include_deleted: bool = False) -> list[Node]:
        statement = select(Node).where(Node.mount_id == mount_id, Node.parent_id == parent_id)
        if not include_deleted:
            statement = statement.where(Node.deleted_at.is_(None))
        result = await self.session.scalars(statement.order_by(Node.type.desc(), Node.name.asc()))
        return list(result)

    async def list_deleted_for_owner(self, owner_id: int) -> list[Node]:
        result = await self.session.scalars(
            select(Node)
            .where(Node.owner_id == owner_id, Node.deleted_at.is_not(None))
            .order_by(Node.deleted_at.desc(), Node.id.desc())
        )
        return list(result)

    async def get_mount_root(self, mount_id: int) -> Node | None:
        result = await self.session.scalars(
            select(Node)
            .where(Node.mount_id == mount_id, Node.parent_id.is_(None), Node.type == NodeType.FOLDER)
            .order_by(Node.id.asc())
        )
        return result.first()

    async def ensure_mount_root(self, *, owner_id: int, mount_id: int, name: str) -> Node:
        root = await self.get_mount_root(mount_id)
        if root is not None:
            return root

        root = Node(
            owner_id=owner_id,
            mount_id=mount_id,
            parent_id=None,
            type=NodeType.FOLDER,
            status=NodeStatus.ACTIVE,
            name=name,
        )
        self.session.add(root)
        await self.session.flush()
        return root

    async def create_folder(self, *, owner_id: int, mount_id: int, parent_id: int | None, name: str) -> Node:
        node = Node(
            owner_id=owner_id,
            mount_id=mount_id,
            parent_id=parent_id,
            type=NodeType.FOLDER,
            status=NodeStatus.ACTIVE,
            name=name,
        )
        self.session.add(node)
        await self.session.flush()
        return node

    async def create_file(
        self,
        *,
        owner_id: int,
        mount_id: int,
        parent_id: int | None,
        name: str,
        blob_path: str,
        size: int,
    ) -> Node:
        node = Node(
            owner_id=owner_id,
            mount_id=mount_id,
            parent_id=parent_id,
            type=NodeType.FILE,
            status=NodeStatus.ACTIVE,
            name=name,
            blob_path=blob_path,
            size=size,
        )
        self.session.add(node)
        await self.session.flush()
        return node

    async def rename(self, node: Node, name: str) -> Node:
        node.name = name
        await self.session.flush()
        return node

    async def collect_descendant_ids(self, node_ids: list[int]) -> list[int]:
        all_ids = set(node_ids)
        queue = list(node_ids)
        while queue:
            current_id = queue.pop(0)
            result = await self.session.scalars(select(Node.id).where(Node.parent_id == current_id))
            child_ids = [child_id for child_id in result if child_id not in all_ids]
            all_ids.update(child_ids)
            queue.extend(child_ids)
        return list(all_ids)

    async def soft_delete(self, node_ids: list[int]) -> list[Node]:
        all_ids = await self.collect_descendant_ids(node_ids)
        result = await self.session.scalars(select(Node).where(Node.id.in_(all_ids)))
        nodes = list(result)
        deleted_at = datetime.now(UTC)
        for node in nodes:
            node.status = NodeStatus.DELETED
            node.deleted_at = deleted_at
        await self.session.flush()
        return nodes

    async def restore(self, node_ids: list[int]) -> list[Node]:
        all_ids = await self.collect_descendant_ids(node_ids)
        result = await self.session.scalars(select(Node).where(Node.id.in_(all_ids)))
        nodes = list(result)
        for node in nodes:
            node.status = NodeStatus.ACTIVE
            node.deleted_at = None
        await self.session.flush()
        return nodes

    async def hard_delete(self, node_ids: list[int]) -> list[int]:
        all_ids = await self.collect_descendant_ids(node_ids)
        result = await self.session.scalars(select(Node).where(Node.id.in_(all_ids)))
        nodes = list(result)
        for node in nodes:
            await self.session.delete(node)
        await self.session.flush()
        return all_ids

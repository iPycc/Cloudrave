"""Virtual file node persistence operations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from models.enums import NodeStatus, NodeType
from models.tables import Node, ObjectRecord


ID_BATCH_SIZE = 500


def _unique_name(name: str, existing: set[str]) -> str:
    if name not in existing:
        return name
    base, dot, ext = name.rpartition(".")
    if not dot:
        base, ext = name, ""
    else:
        ext = dot + ext
    counter = 2
    while True:
        candidate = f"{base} ({counter}){ext}"
        if candidate not in existing:
            return candidate
        counter += 1


class NodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, node_id: int) -> Node | None:
        return await self.session.get(Node, node_id)

    async def get_by_id_for_update(self, node_id: int) -> Node | None:
        return await self.session.scalar(select(Node).where(Node.id == node_id).with_for_update())

    async def list_by_ids(self, node_ids: list[int]) -> list[Node]:
        if not node_ids:
            return []
        unique_ids = list(dict.fromkeys(node_ids))
        nodes: list[Node] = []
        for offset in range(0, len(unique_ids), ID_BATCH_SIZE):
            batch = unique_ids[offset : offset + ID_BATCH_SIZE]
            result = await self.session.scalars(select(Node).where(Node.id.in_(batch)))
            nodes.extend(result)
        return sorted(nodes, key=lambda node: node.id)

    async def list_children(self, mount_id: int, parent_id: int | None, *, include_deleted: bool = False) -> list[Node]:
        statement = select(Node).where(Node.mount_id == mount_id, Node.parent_id == parent_id)
        if not include_deleted:
            statement = statement.where(Node.deleted_at.is_(None))
        result = await self.session.scalars(statement.order_by(Node.type.desc(), Node.name.asc()))
        return list(result)

    async def list_children_page(
        self,
        mount_id: int,
        parent_id: int,
        *,
        limit: int,
        offset: int,
        sort: str,
        folders_only: bool = False,
    ) -> tuple[list[Node], bool]:
        statement = select(Node).where(
            Node.mount_id == mount_id,
            Node.parent_id == parent_id,
            Node.deleted_at.is_(None),
        )
        if folders_only:
            statement = statement.where(Node.type == NodeType.FOLDER)
        return await self._list_page(statement, limit=limit, offset=offset, sort=sort)

    async def list_category_page(
        self,
        mount_id: int,
        category: str,
        *,
        limit: int,
        offset: int,
        sort: str,
    ) -> tuple[list[Node], bool]:
        extensions = {
            "image": ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico", "heic", "tif", "tiff"),
            "video": ("mp4", "mov", "mkv", "avi", "webm", "flv", "m4v", "wmv"),
            "audio": ("mp3", "wav", "flac", "ogg", "m4a", "aac", "opus"),
            "document": (
                "pdf", "doc", "docx", "docm", "dot", "dotx", "xls", "xlsx", "xlsm", "xlsb", "xltx",
                "ppt", "pptx", "pptm", "pps", "ppsx", "csv", "txt",
            ),
        }[category]
        lowered_name = func.lower(Node.name)
        statement = select(Node).where(
            Node.mount_id == mount_id,
            Node.type == NodeType.FILE,
            Node.deleted_at.is_(None),
            or_(*(lowered_name.like(f"%.{extension}") for extension in extensions)),
        )
        return await self._list_page(statement, limit=limit, offset=offset, sort=sort)

    async def search(self, mount_id: int, query: str, *, limit: int) -> list[Node]:
        lowered_query = query.strip().lower()
        result = await self.session.scalars(
            select(Node)
            .where(
                Node.mount_id == mount_id,
                Node.deleted_at.is_(None),
                func.lower(Node.name).contains(lowered_query, autoescape=True),
            )
            .order_by(Node.type.desc(), Node.name.asc(), Node.id.asc())
            .limit(limit)
        )
        return list(result)

    async def _list_page(
        self,
        statement: Select,
        *,
        limit: int,
        offset: int,
        sort: str,
    ) -> tuple[list[Node], bool]:
        if sort == "updated-desc":
            value_order = Node.updated_at.desc()
        elif sort == "updated-asc":
            value_order = Node.updated_at.asc()
        elif sort == "name-desc":
            value_order = Node.name.desc()
        elif sort == "size-desc":
            value_order = Node.size.desc()
        else:
            value_order = Node.name.asc()

        result = await self.session.scalars(
            statement
            .order_by(Node.type.desc(), value_order, Node.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
        nodes = list(result)
        return nodes[:limit], len(nodes) > limit

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

    async def has_mount_data(self, mount_id: int) -> bool:
        """Return whether a mount contains anything beyond its virtual root."""

        node_count = await self.session.scalar(
            select(func.count(Node.id)).where(
                Node.mount_id == mount_id,
                or_(
                    Node.parent_id.is_not(None),
                    Node.type != NodeType.FOLDER,
                    Node.blob_path.is_not(None),
                ),
            )
        )
        if node_count:
            return True
        object_count = await self.session.scalar(
            select(func.count(ObjectRecord.id))
            .join(Node, Node.id == ObjectRecord.node_id)
            .where(Node.mount_id == mount_id)
        )
        return bool(object_count)

    async def mount_content_counts(self, mount_id: int) -> tuple[int, int]:
        """Return tracked file and non-root folder counts for a mount."""

        file_count = await self.session.scalar(
            select(func.count(Node.id)).where(
                Node.mount_id == mount_id,
                Node.type == NodeType.FILE,
            )
        )
        folder_count = await self.session.scalar(
            select(func.count(Node.id)).where(
                Node.mount_id == mount_id,
                Node.type == NodeType.FOLDER,
                Node.parent_id.is_not(None),
            )
        )
        return int(file_count or 0), int(folder_count or 0)

    async def mount_file_bytes(self, mount_id: int) -> int:
        """Return logical file bytes, including recycle-bin items that still consume quota."""

        total = await self.session.scalar(
            select(func.coalesce(func.sum(Node.size), 0)).where(
                Node.mount_id == mount_id,
                Node.type == NodeType.FILE,
            )
        )
        return int(total or 0)

    async def get_child_any(self, mount_id: int, parent_id: int | None, name: str) -> Node | None:
        """Find a child including a previously missing/deleted imported node."""

        return await self.session.scalar(
            select(Node).where(
                Node.mount_id == mount_id,
                Node.parent_id == parent_id,
                Node.name == name,
            )
        )

    async def reactivate_file(self, node: Node, *, blob_path: str, size: int) -> Node:
        node.type = NodeType.FILE
        node.status = NodeStatus.ACTIVE
        node.deleted_at = None
        node.blob_path = blob_path
        node.size = size
        node.updated_at = datetime.now(UTC)
        await self.session.flush()
        return node

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

    async def update_file_metadata(self, node: Node, *, size: int) -> Node:
        node.size = size
        node.updated_at = datetime.now(UTC)
        await self.session.flush()
        return node

    async def ensure_folder(self, *, owner_id: int, mount_id: int, parent_id: int, name: str) -> Node:
        existing = await self.get_child_any(mount_id, parent_id, name)
        if existing is not None:
            if existing.type == NodeType.FOLDER and existing.deleted_at is None:
                return existing
            raise ValueError("Folder path conflicts with an existing node")

        try:
            async with self.session.begin_nested():
                return await self.create_folder(
                    owner_id=owner_id,
                    mount_id=mount_id,
                    parent_id=parent_id,
                    name=name,
                )
        except IntegrityError:
            existing = await self.get_child_any(mount_id, parent_id, name)
            if existing is not None and existing.type == NodeType.FOLDER and existing.deleted_at is None:
                return existing
            raise

    async def collect_descendant_ids(self, node_ids: list[int]) -> list[int]:
        if not node_ids:
            return []

        node_alias = aliased(Node)
        tree = select(Node.id).where(Node.id.in_(node_ids)).cte(recursive=True)
        tree_alias = aliased(tree)
        recursive_query = select(node_alias.id).where(node_alias.parent_id == tree_alias.c.id)
        statement = select(tree.union_all(recursive_query).c.id)
        result = await self.session.scalars(statement)
        return list(result.all())

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

    async def get_child_names(self, mount_id: int, parent_id: int | None) -> set[str]:
        result = await self.session.scalars(
            select(Node.name).where(
                Node.mount_id == mount_id,
                Node.parent_id == parent_id,
                Node.deleted_at.is_(None),
            )
        )
        return set(result.all())

    async def move_nodes(self, nodes: list[Node], target_parent_id: int | None) -> list[Node]:
        if not nodes:
            return []
        mount_id = nodes[0].mount_id
        existing = await self.get_child_names(mount_id, target_parent_id)
        for node in nodes:
            node.parent_id = target_parent_id
            node.name = _unique_name(node.name, existing)
            existing.add(node.name)
        await self.session.flush()
        return nodes

    async def copy_subtree(
        self,
        source_node: Node,
        target_parent_id: int | None,
        *,
        owner_id: int,
        id_map: dict[int, int] | None = None,
    ) -> Node:
        existing = await self.get_child_names(source_node.mount_id, target_parent_id)
        new_name = _unique_name(source_node.name, existing)
        new_node = Node(
            owner_id=owner_id,
            mount_id=source_node.mount_id,
            parent_id=target_parent_id,
            type=source_node.type,
            status=NodeStatus.ACTIVE,
            name=new_name,
            blob_path=source_node.blob_path,
            size=source_node.size,
        )
        self.session.add(new_node)
        await self.session.flush()
        if id_map is not None:
            id_map[source_node.id] = new_node.id
        if source_node.type == NodeType.FOLDER:
            children = await self.list_children(source_node.mount_id, source_node.id)
            for child in children:
                await self.copy_subtree(child, new_node.id, owner_id=owner_id, id_map=id_map)
        return new_node

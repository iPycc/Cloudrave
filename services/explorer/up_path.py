"""Virtual upload target resolution."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

from models.enums import NodeType


class UploadPath:
    def __init__(self, nodes) -> None:
        self.nodes = nodes

    async def target(self, mount, parent_id: int | None, file_name: str, relative_path: str | None):
        parent = await self.parent(mount, parent_id)
        if not relative_path:
            return parent, file_name

        segments = relative_path.split("/")
        for folder_name in segments[:-1]:
            try:
                parent = await self.nodes.ensure_folder(
                    owner_id=mount.owner_id,
                    mount_id=mount.id,
                    parent_id=parent.id,
                    name=folder_name,
                )
            except (ValueError, IntegrityError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Folder path conflicts at {folder_name}",
                ) from exc
        return parent, segments[-1]

    async def parent(self, mount, parent_id: int | None):
        if parent_id is None:
            return await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
        parent = await self.nodes.get_by_id(parent_id)
        if parent is None or parent.mount_id != mount.id or parent.type != NodeType.FOLDER:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found")
        return parent

    async def relative(self, mount, parent_node) -> str | None:
        root = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
        if parent_node.id == root.id:
            return None
        segments: list[str] = []
        current = parent_node
        while current is not None and current.id != root.id:
            segments.append(current.name)
            if current.parent_id is None:
                break
            current = await self.nodes.get_by_id(current.parent_id)
        return "/".join(reversed(segments)) if segments else None

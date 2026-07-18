"""Explorer file services."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import logging
import re
import zipfile
from collections import deque
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NodeType
from app.models.tables import BucketMount, Node, StoragePolicy, User
from app.repositories.audit_logs import AuditLogRepository
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.schemas.mount import BucketMountRead
from app.schemas.node import (
    FileCreateRequest,
    FolderCreateRequest,
    NodeCopyRequest,
    NodeDeleteRequest,
    NodeMoveRequest,
    NodePageRead,
    NodeRead,
    NodeRenameRequest,
    NodeSearchResult,
)
from app.storage.manager import StorageManager
from logging_config import LogOperation, LogOutcome


LOGGER = logging.getLogger("cloudrave.explorer")


def _decode_page_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(f"{cursor}{padding}").decode("ascii"))
    except (ValueError, UnicodeDecodeError, binascii.Error) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid page cursor") from error
    if value < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid page cursor")
    return value


def _encode_page_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii").rstrip("=")


class FileExplorerService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        mounts: BucketMountRepository,
        policies: StoragePolicyRepository,
        nodes: NodeRepository,
        objects: ObjectRepository,
        audits: AuditLogRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.session = session
        self.mounts = mounts
        self.policies = policies
        self.nodes = nodes
        self.objects = objects
        self.audits = audits
        self.storage_manager = storage_manager

    async def list_mounts(self, actor: User) -> list[BucketMountRead]:
        mounts = await self.mounts.list_for_owner(actor.id)
        return [BucketMountRead.model_validate(mount) for mount in mounts]

    async def list_children(self, actor: User, mount_id: int, parent_id: int | None = None) -> list[NodeRead]:
        mount, _policy = await self._require_mount_access(actor, mount_id)
        if parent_id is None:
            root = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
            parent_id = root.id
        nodes = await self.nodes.list_children(mount.id, parent_id, include_deleted=False)
        return [NodeRead.model_validate(node) for node in nodes]

    async def list_children_page(
        self,
        actor: User,
        mount_id: int,
        parent_id: int | None,
        *,
        limit: int,
        cursor: str | None,
        sort: str,
        folders_only: bool,
    ) -> NodePageRead:
        mount, _policy = await self._require_mount_access(actor, mount_id)
        if parent_id is None:
            root = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
            parent_id = root.id
        offset = _decode_page_cursor(cursor)
        nodes, has_more = await self.nodes.list_children_page(
            mount.id,
            parent_id,
            limit=limit,
            offset=offset,
            sort=sort,
            folders_only=folders_only,
        )
        return NodePageRead(
            items=[NodeRead.model_validate(node) for node in nodes],
            next_cursor=_encode_page_cursor(offset + limit) if has_more else None,
        )

    async def list_category_page(
        self,
        actor: User,
        mount_id: int,
        category: str,
        *,
        limit: int,
        cursor: str | None,
        sort: str,
    ) -> NodePageRead:
        mount, _policy = await self._require_mount_access(actor, mount_id)
        offset = _decode_page_cursor(cursor)
        nodes, has_more = await self.nodes.list_category_page(
            mount.id,
            category,
            limit=limit,
            offset=offset,
            sort=sort,
        )
        return NodePageRead(
            items=[NodeRead.model_validate(node) for node in nodes],
            next_cursor=_encode_page_cursor(offset + limit) if has_more else None,
        )

    async def search_nodes(
        self,
        actor: User,
        mount_id: int,
        query: str,
        *,
        limit: int,
    ) -> list[NodeSearchResult]:
        mount, _policy = await self._require_mount_access(actor, mount_id)
        nodes = await self.nodes.search(mount.id, query, limit=limit)
        parent_cache: dict[int, Node] = {}
        results: list[NodeSearchResult] = []

        for node in nodes:
            names: deque[str] = deque()
            parent_id = node.parent_id
            while parent_id is not None:
                parent = parent_cache.get(parent_id)
                if parent is None:
                    parent = await self.nodes.get_by_id(parent_id)
                    if parent is not None:
                        parent_cache[parent_id] = parent
                if parent is None or parent.parent_id is None:
                    break
                names.appendleft(parent.name)
                parent_id = parent.parent_id

            parent_path = f"/{'/'.join(names)}" if names else ""
            results.append(
                NodeSearchResult(
                    node=NodeRead.model_validate(node),
                    parent_path=parent_path,
                )
            )

        return results

    async def create_folder(self, request: Request, actor: User, payload: FolderCreateRequest) -> NodeRead:
        mount, _policy = await self._require_mount_access(actor, payload.mount_id)
        parent = None
        if payload.parent_id is not None:
            parent = await self.nodes.get_by_id(payload.parent_id)
            if parent is None or parent.mount_id != mount.id or parent.type != NodeType.FOLDER:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found")
        else:
            parent = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)

        # Resolve unique folder name: append (2), (3), ... if conflict
        folder_name = payload.name
        siblings = await self.nodes.list_children(mount.id, parent.id if parent else None, include_deleted=False)
        sibling_names = {s.name for s in siblings}
        if folder_name in sibling_names:
            base = re.sub(r"\s*\(\d+\)$", "", folder_name)
            counter = 2
            while f"{base} ({counter})" in sibling_names:
                counter += 1
            folder_name = f"{base} ({counter})"

        node = await self.nodes.create_folder(
            owner_id=mount.owner_id,
            mount_id=mount.id,
            parent_id=parent.id if parent else None,
            name=folder_name,
        )
        await self.audits.create(
            actor_id=actor.id,
            action="node.create_folder",
            target=f"node:{node.id}",
            detail=node.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Created folder {node.name}",
            extra={
                "operation": LogOperation.CREATE,
                "resource": "folder",
                "outcome": LogOutcome.SUCCESS,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return NodeRead.model_validate(node)

    async def create_file(self, request: Request, actor: User, payload: FileCreateRequest) -> NodeRead:
        mount, policy = await self._require_mount_access(actor, payload.mount_id)
        parent = await self._resolve_parent_folder(mount, payload.parent_id)
        requested_name = payload.name.strip()
        if "/" in requested_name or "\\" in requested_name or requested_name in {".", ".."}:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file name")
        extension = Path(requested_name).suffix.lower()
        if extension not in {".txt", ".md", ".markdown"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only TXT and Markdown files can be created here",
            )

        siblings = await self.nodes.list_children(mount.id, parent.id, include_deleted=True)
        sibling_names = {sibling.name for sibling in siblings}
        file_name = requested_name
        if file_name in sibling_names:
            stem = Path(file_name).stem
            suffix = Path(file_name).suffix
            base = re.sub(r"\s*\(\d+\)$", "", stem)
            counter = 2
            while f"{base} ({counter}){suffix}" in sibling_names:
                counter += 1
            file_name = f"{base} ({counter}){suffix}"

        parent_path = await self._build_parent_relative_path(mount, parent)
        storage_name = f"{uuid4().hex}-{file_name}"
        object_key = self.storage_manager.build_object_key(mount, storage_name, parent_path)
        content_type = "text/markdown; charset=utf-8" if extension in {".md", ".markdown"} else "text/plain; charset=utf-8"
        adapter = self.storage_manager.get_adapter(policy)

        object_written = False

        async def cleanup_object() -> None:
            if not object_written:
                return
            try:
                await asyncio.to_thread(adapter.delete_object, policy, mount, object_key, hard_delete=True)
            except Exception:
                LOGGER.exception("Failed to clean up object after text file creation failed", extra={"object_key": object_key})

        try:
            await asyncio.to_thread(
                adapter.put_object,
                policy,
                mount,
                object_key,
                b"",
                mime_type=content_type,
            )
            object_written = True
            node = await self.nodes.create_file(
                owner_id=mount.owner_id,
                mount_id=mount.id,
                parent_id=parent.id,
                name=file_name,
                blob_path=object_key,
                size=0,
            )
            await self.objects.create_or_update(
                node_id=node.id,
                policy_id=policy.id,
                blob_path=object_key,
                checksum=hashlib.sha256(b"").hexdigest(),
                content_type=content_type,
            )
            await self.audits.create(
                actor_id=actor.id,
                action="node.create_file",
                target=f"node:{node.id}",
                detail=node.name,
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
        except IntegrityError as error:
            await cleanup_object()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A file with this name already exists") from error
        except Exception:
            await cleanup_object()
            raise
        LOGGER.info(
            f"Created file {node.name}",
            extra={
                "operation": LogOperation.CREATE,
                "resource": "file",
                "outcome": LogOutcome.SUCCESS,
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return NodeRead.model_validate(node)

    async def rename_node(self, request: Request, actor: User, node_id: int, payload: NodeRenameRequest) -> NodeRead:
        node = await self._require_node_access(actor, node_id)
        updated = await self.nodes.rename(node, payload.name)
        await self.audits.create(
            actor_id=actor.id,
            action="node.rename",
            target=f"node:{updated.id}",
            detail=updated.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return NodeRead.model_validate(updated)

    async def delete_nodes(self, request: Request, actor: User, payload: NodeDeleteRequest) -> dict[str, list[int] | list[NodeRead]]:
        nodes = [await self._require_node_access(actor, node_id) for node_id in payload.node_ids]
        _ = nodes
        if payload.hard_delete:
            all_ids = await self.nodes.collect_descendant_ids(payload.node_ids)
            descendants = await self.nodes.list_by_ids(all_ids)

            file_nodes = [node for node in descendants if node.type == NodeType.FILE and node.blob_path]
            mount_ids = {node.mount_id for node in file_nodes}
            mounts = {mount.id: mount for mount in await self.mounts.list_by_ids(list(mount_ids))}
            policy_ids = {mount.policy_id for mount in mounts.values()}
            policies = {policy.id: policy for policy in await self.policies.list_by_ids(list(policy_ids))}

            async def _delete_one(node: Node) -> None:
                mount = mounts.get(node.mount_id)
                if mount is None:
                    return
                policy = policies.get(mount.policy_id)
                if policy is None:
                    return
                adapter = self.storage_manager.get_adapter(policy)
                # Local filesystem IO can block the event loop; run it in a thread.
                await asyncio.to_thread(adapter.delete_object, policy, mount, node.blob_path, hard_delete=True)

            if file_nodes:
                await asyncio.gather(*(_delete_one(node) for node in file_nodes))

            await self.objects.delete_by_node_ids(all_ids)
            await self.nodes.hard_delete(payload.node_ids)
            await self.audits.create(
                actor_id=actor.id,
                action="node.hard_delete",
                target=f"nodes:{','.join(str(node_id) for node_id in all_ids)}",
                detail="hard_delete",
                ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_id=getattr(request.state, "request_id", None),
            )
            LOGGER.info(
                f"Hard deleted {len(all_ids)} node(s)",
                extra={
                    "operation": LogOperation.DELETE,
                    "resource": "node",
                    "outcome": LogOutcome.SUCCESS,
                    "scope": "permanent",
                    "count": len(all_ids),
                    "request_id": getattr(request.state, "request_id", "-"),
                    "ip": request.client.host if request.client else "-",
                    "actor": actor.email,
                },
            )
            return {"deleted_node_ids": all_ids}

        deleted = await self.nodes.soft_delete(payload.node_ids)
        await self.audits.create(
            actor_id=actor.id,
            action="node.soft_delete",
            target=f"nodes:{','.join(str(node.id) for node in deleted)}",
            detail="soft_delete",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        LOGGER.info(
            f"Moved {len(deleted)} node(s) to the recycle bin",
            extra={
                "operation": LogOperation.RECYCLE,
                "resource": "node",
                "outcome": LogOutcome.SUCCESS,
                "count": len(deleted),
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": actor.email,
            },
        )
        return {"nodes": [NodeRead.model_validate(node) for node in deleted]}

    async def move_nodes(self, request: Request, actor: User, payload: NodeMoveRequest) -> list[NodeRead]:
        nodes = [await self._require_node_access(actor, node_id) for node_id in payload.node_ids]
        if not nodes:
            return []
        mount_id = nodes[0].mount_id
        mount, _policy = await self._require_mount_access(actor, mount_id)

        target_parent_id = payload.target_parent_id
        if target_parent_id is not None:
            target = await self.nodes.get_by_id(target_parent_id)
            if target is None or target.mount_id != mount_id or target.type != NodeType.FOLDER:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target folder not found")

        # Prevent moving a folder into its own subtree
        moved_ids = {node.id for node in nodes}
        if target_parent_id in moved_ids:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot move a folder into itself")
        for node in nodes:
            if node.type == NodeType.FOLDER:
                descendants = await self.nodes.collect_descendant_ids([node.id])
                if target_parent_id in descendants:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Cannot move a folder into its own subtree",
                    )

        moved = await self.nodes.move_nodes(nodes, target_parent_id)
        await self.audits.create(
            actor_id=actor.id,
            action="node.move",
            target=f"nodes:{','.join(str(node.id) for node in moved)}",
            detail=f"parent:{target_parent_id}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return [NodeRead.model_validate(node) for node in moved]

    async def copy_nodes(self, request: Request, actor: User, payload: NodeCopyRequest) -> list[NodeRead]:
        nodes = [await self._require_node_access(actor, node_id) for node_id in payload.node_ids]
        if not nodes:
            return []
        mount_id = nodes[0].mount_id
        mount, _policy = await self._require_mount_access(actor, mount_id)

        target_parent_id = payload.target_parent_id
        if target_parent_id is not None:
            target = await self.nodes.get_by_id(target_parent_id)
            if target is None or target.mount_id != mount_id or target.type != NodeType.FOLDER:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target folder not found")

        copied: list[NodeRead] = []
        id_map: dict[int, int] = {}
        source_object_ids = [node.id for node in nodes if node.type == NodeType.FILE and node.blob_path]
        source_objects = {obj.node_id: obj for obj in await self.objects.get_by_node_ids(source_object_ids)}

        for node in nodes:
            new_node = await self.nodes.copy_subtree(node, target_parent_id, owner_id=actor.id, id_map=id_map)
            copied.append(NodeRead.model_validate(new_node))
            if node.type == NodeType.FILE and node.id in source_objects:
                source_obj = source_objects[node.id]
                await self.objects.create_or_update(
                    node_id=new_node.id,
                    policy_id=source_obj.policy_id,
                    blob_path=source_obj.blob_path,
                    checksum=source_obj.checksum,
                    content_type=source_obj.content_type,
                    extra=source_obj.extra,
                )

        await self.audits.create(
            actor_id=actor.id,
            action="node.copy",
            target=f"nodes:{','.join(str(node.id) for node in copied)}",
            detail=f"parent:{target_parent_id}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return copied

    async def download_folder(self, request: Request, actor: User, node_id: int) -> StreamingResponse:
        node = await self._require_node_access(actor, node_id)
        if node.type != NodeType.FOLDER:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Node is not a folder")
        mount, policy = await self._require_mount_access(actor, node.mount_id)

        all_ids = await self.nodes.collect_descendant_ids([node_id])
        descendants = await self.nodes.list_by_ids(all_ids)
        file_nodes = [n for n in descendants if n.type == NodeType.FILE and n.blob_path]
        if not file_nodes:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Folder is empty")

        # Build relative archive paths
        node_map = {n.id: n for n in descendants}
        node_map[node.id] = node
        entries: list[tuple[str, Node]] = []
        for file_node in file_nodes:
            parts: list[str] = []
            current: Node | None = file_node
            while current is not None and current.id != node.id:
                parts.append(current.name)
                current = node_map.get(current.parent_id) if current.parent_id is not None else None
            rel_path = "/".join(reversed(parts))
            entries.append((rel_path, file_node))

        adapter = self.storage_manager.get_adapter(policy)
        chunk_size = 64 * 1024

        def _build_zip_in_thread() -> bytes:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for rel_path, file_node in entries:
                    stream = adapter.open_object(policy, mount, file_node.blob_path)
                    data = stream.read() if hasattr(stream, "read") else b"".join(stream)
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                    zf.writestr(rel_path, data)
            return buffer.getvalue()

        async def _stream() -> AsyncIterator[bytes]:
            archive = await asyncio.to_thread(_build_zip_in_thread)
            for offset in range(0, len(archive), chunk_size):
                yield archive[offset : offset + chunk_size]

        await self.audits.create(
            actor_id=actor.id,
            action="node.download_folder",
            target=f"node:{node.id}",
            detail=node.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        safe_name = node.name.replace('"', "")
        return StreamingResponse(
            _stream(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
        )

    async def _require_mount_access(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        mount = await self.mounts.get_by_id(mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        if mount.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Mount access denied")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return mount, policy

    async def _resolve_parent_folder(self, mount: BucketMount, parent_id: int | None) -> Node:
        if parent_id is None:
            return await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
        parent = await self.nodes.get_by_id(parent_id)
        if parent is None or parent.mount_id != mount.id or parent.type != NodeType.FOLDER:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found")
        return parent

    async def _build_parent_relative_path(self, mount: BucketMount, parent: Node) -> str | None:
        root = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
        if parent.id == root.id:
            return None
        segments: list[str] = []
        current: Node | None = parent
        while current is not None and current.id != root.id:
            segments.append(current.name)
            current = await self.nodes.get_by_id(current.parent_id) if current.parent_id is not None else None
        return "/".join(reversed(segments)) if segments else None

    async def _require_node_access(self, actor: User, node_id: int):
        node = await self.nodes.get_by_id(node_id)
        if node is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        if node.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Node access denied")
        return node

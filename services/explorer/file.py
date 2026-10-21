"""Explorer file services."""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from collections import deque
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.enums import NodeType
from models.tables import BucketMount, Node, StoragePolicy, User
from repositories.audit_logs import AuditLogRepository
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from schemas.mount import BucketMountRead, mount_read
from schemas.node import (
    FileCreateRequest,
    FolderCreateRequest,
    NodeCopyRequest,
    NodeActivityRead,
    NodeDeleteRequest,
    NodeMoveRequest,
    NodePageRead,
    NodePageMetadataRead,
    NodeRead,
    NodeRenameRequest,
    NodeSearchResult,
)
from services.explorer.archive import ArchiveDownloadService
from services.explorer.access import MountAccessPolicy
from services.explorer.gc import ObjectGc
from services.user.response import build_avatar_url
from store.call import call_adapter
from store.key import managed_key
from store.manager import StorageManager
from core.log import LogOperation, LogOutcome
from security.capabilities import Capability, require_capability


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


def _next_folder_name(name: str, existing_names: set[str]) -> str:
    """Return a sibling-safe folder name using the familiar ``(2)`` suffix."""

    if name not in existing_names:
        return name

    base = re.sub(r"\s*\(\d+\)$", "", name)
    counter = 2
    while f"{base} ({counter})" in existing_names:
        counter += 1
    return f"{base} ({counter})"


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
        self.mount_access = MountAccessPolicy(mounts=mounts, policies=policies)

    async def list_mounts(self, actor: User) -> list[BucketMountRead]:
        mounts = await self.mounts.list_for_owner(actor.id)
        policies = {
            policy.id: policy
            for policy in await self.policies.list_by_ids([mount.policy_id for mount in mounts])
        }
        return [mount_read(mount, policies.get(mount.policy_id), self.storage_manager) for mount in mounts]

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
        folder_count, file_count = await self.nodes.child_content_counts(mount.id, parent_id)
        if folders_only:
            file_count = 0
        return NodePageRead(
            items=[NodeRead.model_validate(node) for node in nodes],
            next_cursor=_encode_page_cursor(offset + limit) if has_more else None,
            total=folder_count + file_count,
            folder_count=folder_count,
            file_count=file_count,
        )

    async def child_metadata(
        self,
        actor: User,
        mount_id: int,
        parent_id: int | None,
        *,
        folders_only: bool,
    ) -> NodePageMetadataRead:
        mount, _policy = await self._require_mount_access(actor, mount_id)
        if parent_id is None:
            root = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
            parent_id = root.id
        folder_count, file_count = await self.nodes.child_content_counts(mount.id, parent_id)
        if folders_only:
            file_count = 0
        return NodePageMetadataRead(
            total=folder_count + file_count,
            folder_count=folder_count,
            file_count=file_count,
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
        total = await self.nodes.category_count(mount.id, category)
        return NodePageRead(
            items=[NodeRead.model_validate(node) for node in nodes],
            next_cursor=_encode_page_cursor(offset + limit) if has_more else None,
            total=total,
            folder_count=0,
            file_count=total,
        )

    async def category_metadata(
        self,
        actor: User,
        mount_id: int,
        category: str,
    ) -> NodePageMetadataRead:
        mount, _policy = await self._require_mount_access(actor, mount_id)
        total = await self.nodes.category_count(mount.id, category)
        return NodePageMetadataRead(total=total, folder_count=0, file_count=total)

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

    async def list_activity(self, actor: User, node_id: int) -> list[NodeActivityRead]:
        await self._require_node_access(actor, node_id)
        rows = await self.audits.list_node_events(node_id)
        return [
            NodeActivityRead(
                id=log.id,
                action=log.action,
                actor_id=log.actor_id,
                actor_name=user.username if user is not None else "系统",
                actor_avatar=build_avatar_url(user) if user is not None else None,
                detail=log.detail,
                created_at=log.created_at,
            )
            for log, user in rows
        ]

    async def record_open(self, request: Request, actor: User, node_id: int) -> None:
        node = await self._require_node_access(actor, node_id)
        await self.audits.create(
            actor_id=actor.id,
            action="file.open",
            target=f"node:{node.id}",
            detail=node.name,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )

    async def create_folder(self, request: Request, actor: User, payload: FolderCreateRequest) -> NodeRead:
        mount, _policy = await self._require_mount_write(actor, payload.mount_id)
        parent = None
        if payload.parent_id is not None:
            parent = await self.nodes.get_by_id(payload.parent_id)
            if parent is None or parent.mount_id != mount.id or parent.type != NodeType.FOLDER:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found")
        else:
            parent = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)

        # Deleted nodes still participate in the database's sibling-name unique
        # constraint. Include them here so creating a folder never fails merely
        # because the same name exists in the recycle bin.
        parent_id = parent.id if parent else None
        for _attempt in range(5):
            siblings = await self.nodes.list_children(mount.id, parent_id, include_deleted=True)
            folder_name = _next_folder_name(payload.name, {sibling.name for sibling in siblings})
            try:
                # The savepoint keeps the outer request transaction usable if a
                # simultaneous create wins the race for the same suffix.
                async with self.session.begin_nested():
                    node = await self.nodes.create_folder(
                        owner_id=mount.owner_id,
                        mount_id=mount.id,
                        parent_id=parent_id,
                        name=folder_name,
                    )
                break
            except IntegrityError:
                continue
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Unable to allocate a unique folder name",
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
        require_capability(actor, Capability.FILE_EDIT)
        mount, policy = await self._require_mount_write(actor, payload.mount_id)
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

        object_key = managed_key(
            actor.id,
            file_name,
            style=str((mount.extra or {}).get("object_key_style", "readable")),
        )
        content_type = "text/markdown; charset=utf-8" if extension in {".md", ".markdown"} else "text/plain; charset=utf-8"
        adapter = self.storage_manager.get_adapter(policy)

        object_written = False

        async def cleanup_object() -> None:
            if not object_written:
                return
            try:
                await call_adapter(adapter, "delete_object", policy, mount, object_key, hard_delete=True)
            except Exception:
                LOGGER.exception("Failed to clean up object after text file creation failed", extra={"object_key": object_key})

        try:
            await call_adapter(
                adapter,
                "put_object",
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
                physical_id=adapter.physical_id(policy, mount, object_key),
                revive_physical=True,
                checksum=hashlib.sha256(b"").hexdigest(),
                content_type=content_type,
                extra={"origin": "managed_create"},
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
        await self._require_mount_write(actor, node.mount_id)
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
        for mount_id in {node.mount_id for node in nodes}:
            await self._require_mount_write(actor, mount_id)
        if payload.hard_delete:
            all_ids = await self.nodes.collect_descendant_ids(payload.node_ids)
            gc = ObjectGc(
                mounts=self.mounts,
                nodes=self.nodes,
                policies=self.policies,
                objects=self.objects,
                storage_manager=self.storage_manager,
            )
            task_ids = await gc.stage(all_ids)
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
            await self.session.commit()
            await gc.run(task_ids)
            await self.session.commit()
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
        mount, policy = await self._require_mount_write(actor, mount_id)
        if any(node.mount_id != mount_id for node in nodes):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="All moved nodes must belong to one mount")

        target_parent_id = payload.target_parent_id
        if target_parent_id is None:
            target = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
            target_parent_id = target.id
        else:
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
        require_capability(actor, Capability.FILE_COPY)
        nodes = [await self._require_node_access(actor, node_id) for node_id in payload.node_ids]
        if not nodes:
            return []
        mount_id = nodes[0].mount_id
        mount, policy = await self._require_mount_write(actor, mount_id)
        if any(node.mount_id != mount_id for node in nodes):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="All copied nodes must belong to one mount")

        target_parent_id = payload.target_parent_id
        if target_parent_id is None:
            target = await self.nodes.ensure_mount_root(owner_id=mount.owner_id, mount_id=mount.id, name=mount.name)
            target_parent_id = target.id
        else:
            target = await self.nodes.get_by_id(target_parent_id)
            if target is None or target.mount_id != mount_id or target.type != NodeType.FOLDER:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target folder not found")

        copied: list[NodeRead] = []
        id_map: dict[int, int] = {}
        source_ids = await self.nodes.collect_descendant_ids([node.id for node in nodes])
        source_object_ids = [node.id for node in await self.nodes.list_by_ids(source_ids) if node.type == NodeType.FILE and node.blob_path]
        source_objects = {obj.node_id: obj for obj in await self.objects.get_by_node_ids(source_object_ids)}
        adapter = self.storage_manager.get_adapter(policy)

        for node in nodes:
            new_node = await self.nodes.copy_subtree(node, target_parent_id, owner_id=actor.id, id_map=id_map)
            copied.append(NodeRead.model_validate(new_node))

        for source_id, copied_id in id_map.items():
            source_obj = source_objects.get(source_id)
            if source_obj is None:
                continue
            await self.objects.create_or_update(
                node_id=copied_id,
                policy_id=source_obj.policy_id,
                blob_path=source_obj.blob_path,
                physical_id=(
                    source_obj.physical_id
                    or adapter.physical_id(policy, mount, source_obj.blob_path)
                ),
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
        return await self.download_nodes(request, actor, [node_id], filename=node.name)

    async def download_nodes(
        self,
        request: Request,
        actor: User,
        node_ids: list[int],
        *,
        filename: str,
    ) -> StreamingResponse:
        archive_service = ArchiveDownloadService(
            mounts=self.mounts,
            nodes=self.nodes,
            policies=self.policies,
            storage_manager=self.storage_manager,
        )
        roots = await archive_service.resolve_owned_roots(actor, node_ids)
        if request.method == "HEAD":
            return await archive_service.probe_response(roots, filename=filename)
        response = await archive_service.build_archive_response(roots, filename=filename)
        await self.audits.create(
            actor_id=actor.id,
            action="node.download_archive",
            target=f"nodes:{','.join(str(node.id) for node in roots)}",
            detail=filename,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        )
        return response

    async def _require_mount_access(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        return await self.mount_access.read(actor, mount_id)

    async def _require_mount_write(self, actor: User, mount_id: int) -> tuple[BucketMount, StoragePolicy]:
        return await self.mount_access.write(actor, mount_id)

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

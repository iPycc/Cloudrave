"""Preview and download operations for mounted shares."""

from __future__ import annotations

import mimetypes
from contextlib import closing

from fastapi import HTTPException, Request, status

from models.enums import NodeType
from models.tables import User
from repositories.nodes import _unique_name
from services.explorer.archive import ArchiveService
from services.explorer.preview import build_local_preview_response
from services.explorer.proxy import remote_response
from services.explorer.shared import SharedService
from services.explorer.source import FileSourceService
from store.call import call_adapter
from store.key import managed_key


class SharedIo:
    def __init__(self, *, shared: SharedService, source: FileSourceService, archive: ArchiveService) -> None:
        self.shared = shared
        self.source = source
        self.archive = archive

    async def preview(self, request: Request, actor: User, mount_id: int, node_id: int):
        _share, node = await self.shared.resolve_node(actor, mount_id, node_id)
        if node.type != NodeType.FILE:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Folders cannot be previewed")
        node, mount, policy = await self.source.resolve_shared(node.id)
        adapter = self.source.storage_manager.get_adapter(policy)
        local_path = self.source.storage_manager.local_path(policy, mount, node.blob_path)
        if local_path is not None:
            return build_local_preview_response(
                request,
                local_path,
                filename=node.name,
                media_type=mimetypes.guess_type(node.name)[0] or "application/octet-stream",
                cache_control="private, no-store",
            )
        return await remote_response(
            request,
            adapter=adapter,
            policy=policy,
            mount=mount,
            object_key=node.blob_path,
            filename=node.name,
        )

    async def download(self, request: Request, actor: User, mount_id: int, node_id: int):
        share, node = await self.shared.resolve_node(actor, mount_id, node_id)
        if node.type == NodeType.FILE:
            resolved = await self.source.resolve_shared(node.id)
            if request.method == "HEAD":
                return await self.source.probe_response(*resolved)
            response = await self.source.response(*resolved, request=request)
        else:
            if request.method == "HEAD":
                return await self.archive.probe_response([node], filename=node.name)
            response = await self.archive.response([node], filename=node.name)
        await self.shared.source.record_download(share)
        return response

    async def save(
        self,
        actor: User,
        mount_id: int,
        node_id: int,
        target_mount_id: int,
        target_parent_id: int | None,
    ):
        _share, source_node = await self.shared.resolve_node(actor, mount_id, node_id)
        target_mount = await self.source.mounts.get_by_id(target_mount_id)
        if target_mount is None or target_mount.owner_id != actor.id or not target_mount.is_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target mount not found")
        if bool((target_mount.extra or {}).get("read_only")):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Target mount is read-only")
        target_policy = await self.source.policies.get_by_id(target_mount.policy_id)
        if target_policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target storage policy not found")
        if target_parent_id is None:
            parent = await self.source.nodes.ensure_mount_root(
                owner_id=actor.id,
                mount_id=target_mount.id,
                name=target_mount.name,
            )
        else:
            parent = await self.source.nodes.get_by_id(target_parent_id)
            if (
                parent is None
                or parent.owner_id != actor.id
                or parent.mount_id != target_mount.id
                or parent.type != NodeType.FOLDER
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target folder not found")
        return await self._copy_shared_node(actor, source_node, target_mount, target_policy, parent.id)

    async def _copy_shared_node(self, actor: User, source_node, target_mount, target_policy, target_parent_id: int):
        existing = await self.source.nodes.get_child_names(target_mount.id, target_parent_id)
        name = _unique_name(source_node.name, existing)
        if source_node.type == NodeType.FOLDER:
            created = await self.source.nodes.create_folder(
                owner_id=actor.id,
                mount_id=target_mount.id,
                parent_id=target_parent_id,
                name=name,
            )
            for child in await self.source.nodes.list_children(source_node.mount_id, source_node.id):
                await self._copy_shared_node(actor, child, target_mount, target_policy, created.id)
            return created

        resolved_node, source_mount, source_policy = await self.source.resolve_shared(source_node.id)
        source_adapter = self.source.storage_manager.get_adapter(source_policy)
        target_adapter = self.source.storage_manager.get_adapter(target_policy)
        object_key = managed_key(
            actor.id,
            name,
            style=str((target_mount.extra or {}).get("object_key_style", "readable")),
        )
        stream = await call_adapter(source_adapter, "open_object", source_policy, source_mount, resolved_node.blob_path)
        with closing(stream):
            stored = await call_adapter(
                target_adapter,
                "put_object",
                target_policy,
                target_mount,
                object_key,
                stream,
                mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                metadata={"origin": "shared_save"},
            )
        created = await self.source.nodes.create_file(
            owner_id=actor.id,
            mount_id=target_mount.id,
            parent_id=target_parent_id,
            name=name,
            blob_path=object_key,
            size=source_node.size,
        )
        await self.source.objects.create_or_update(
            node_id=created.id,
            policy_id=target_policy.id,
            blob_path=object_key,
            physical_id=target_adapter.physical_id(target_policy, target_mount, object_key),
            revive_physical=True,
            content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            extra={
                "origin": "shared_save",
                "etag": getattr(stored, "etag", None),
                "version_id": getattr(stored, "version_id", None),
            },
        )
        return created

"""Resolve virtual files into local streams or provider URLs."""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from models.enums import NodeType
from models.tables import User
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.objects import ObjectRepository
from repositories.policies import StoragePolicyRepository
from store.call import call_adapter
from store.manager import StorageManager

if TYPE_CHECKING:
    from models.tables import BucketMount, Node, StoragePolicy


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """Build an RFC 5987 filename header that Starlette can encode safely."""

    clean_name = filename.replace("\r", "").replace("\n", "").replace('"', "")
    path = PurePosixPath(clean_name)
    ascii_stem = path.stem.encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_stem).strip(" .") or "download"
    ascii_suffix = path.suffix.encode("ascii", "ignore").decode("ascii")
    ascii_suffix = re.sub(r"[^A-Za-z0-9.]+", "", ascii_suffix)
    encoded_name = quote(clean_name, safe="")
    disposition = "inline" if inline else "attachment"
    return (
        f'{disposition}; filename="{ascii_stem}{ascii_suffix}"; '
        f"filename*=UTF-8''{encoded_name}"
    )


# Compatibility for existing callers and extensions.
build_content_disposition = content_disposition


class FileSourceService:
    """Authorize and serve one physical object behind a virtual file node."""

    def __init__(
        self,
        *,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        policies: StoragePolicyRepository,
        objects: ObjectRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.mounts = mounts
        self.nodes = nodes
        self.policies = policies
        self.objects = objects
        self.storage_manager = storage_manager

    async def resolve(self, actor: User, node_id: int) -> tuple["Node", "BucketMount", "StoragePolicy"]:
        node = await self.nodes.get_by_id(node_id)
        if node is None or node.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        return await self._resolve(node)

    async def resolve_shared(self, node_id: int) -> tuple["Node", "BucketMount", "StoragePolicy"]:
        node = await self.nodes.get_by_id(node_id)
        if node is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        return await self._resolve(node)

    async def _resolve(self, node: "Node") -> tuple["Node", "BucketMount", "StoragePolicy"]:
        if node.deleted_at is not None or node.type != NodeType.FILE or not node.blob_path:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        mount = await self.mounts.get_by_id(node.mount_id)
        if mount is None or not getattr(mount, "is_enabled", True):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None or getattr(policy, "status", "active") != "active":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return node, mount, policy

    def local_response(
        self,
        node: "Node",
        mount: "BucketMount",
        policy: "StoragePolicy",
        *,
        inline: bool = False,
    ) -> FileResponse | StreamingResponse:
        adapter = self.storage_manager.get_adapter(policy)
        content_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
        stream = adapter.open_object(policy, mount, node.blob_path)
        local_path = getattr(stream, "name", None)
        if isinstance(local_path, (str, os.PathLike)):
            stream.close()
            return FileResponse(
                path=local_path,
                media_type=content_type,
                filename=node.name,
                content_disposition_type="inline" if inline else "attachment",
                headers={"Cache-Control": "private, no-store"},
            )
        return StreamingResponse(
            stream,
            media_type=content_type,
            headers={
                "Content-Disposition": content_disposition(node.name, inline=inline),
                "Cache-Control": "private, no-store",
            },
        )

    def build_file_stream(
        self,
        node: "Node",
        mount: "BucketMount",
        policy: "StoragePolicy",
    ) -> FileResponse | StreamingResponse:
        """Compatibility wrapper for the old local-only synchronous call."""

        if self.storage_manager.local_path(policy, mount, node.blob_path) is None:
            raise RuntimeError("Remote storage responses must be awaited with response()")
        return self.local_response(node, mount, policy)

    async def response(
        self,
        node: "Node",
        mount: "BucketMount",
        policy: "StoragePolicy",
        *,
        inline: bool = False,
        expires_in: int | None = None,
    ) -> FileResponse | StreamingResponse | RedirectResponse:
        if self.storage_manager.local_path(policy, mount, node.blob_path) is not None:
            return self.local_response(node, mount, policy, inline=inline)

        adapter = self.storage_manager.get_adapter(policy)
        signed = await call_adapter(
            adapter,
            "build_download_url",
            policy,
            mount,
            node.blob_path,
            expires_in=expires_in,
            download_name=node.name,
            inline=inline,
        )
        return RedirectResponse(
            signed.url,
            headers={"Cache-Control": "private, max-age=30" if inline else "private, no-store"},
        )


# The old name remains import-compatible while routes migrate to source.py.
DownloadFileService = FileSourceService

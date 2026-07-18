"""Download services."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import os
import re
import tempfile
import zipfile
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse

from app.models.enums import NodeType, StoragePolicyType
from app.models.tables import User
from app.repositories.mounts import BucketMountRepository
from app.repositories.nodes import NodeRepository
from app.repositories.objects import ObjectRepository
from app.repositories.policies import StoragePolicyRepository
from app.storage.manager import StorageManager

if TYPE_CHECKING:
    from app.models.tables import BucketMount, Node, StoragePolicy


ARCHIVE_CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_ROOTS = 100


def build_content_disposition(filename: str) -> str:
    """Build an attachment header that Starlette can encode as latin-1.

    The ASCII ``filename`` is a compatibility fallback. ``filename*`` carries
    the real UTF-8 name according to RFC 5987, so Chinese and other Unicode
    names never reach Starlette's latin-1 encoder directly.
    """

    clean_name = filename.replace("\r", "").replace("\n", "").replace('"', "")
    path = PurePosixPath(clean_name)
    ascii_stem = path.stem.encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_stem).strip(" .") or "download"
    ascii_suffix = path.suffix.encode("ascii", "ignore").decode("ascii")
    ascii_suffix = re.sub(r"[^A-Za-z0-9.]+", "", ascii_suffix)
    ascii_name = f"{ascii_stem}{ascii_suffix}"
    encoded_name = quote(clean_name, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'


def _safe_archive_segment(value: str, fallback: str = "item") -> str:
    segment = value.replace("\\", "_").replace("/", "_").replace("\x00", "")
    segment = segment.replace("\r", " ").replace("\n", " ").strip()
    if segment in {"", ".", ".."}:
        return fallback
    return segment


def _unique_archive_path(path: str, used_paths: set[str], *, directory: bool) -> str:
    normalized = str(PurePosixPath(path)).rstrip("/")
    candidate = normalized
    counter = 2
    while candidate.casefold() in used_paths:
        parent, separator, name = normalized.rpartition("/")
        if directory:
            next_name = f"{name} ({counter})"
        else:
            stem, dot, extension = name.rpartition(".")
            next_name = f"{stem} ({counter}).{extension}" if dot and stem else f"{name} ({counter})"
        candidate = f"{parent}{separator}{next_name}"
        counter += 1
    used_paths.add(candidate.casefold())
    return f"{candidate}/" if directory else candidate


class ArchiveDownloadService:
    """Create one ZIP download from files, folders, or a mixed selection."""

    def __init__(
        self,
        *,
        mounts: BucketMountRepository,
        nodes: NodeRepository,
        policies: StoragePolicyRepository,
        storage_manager: StorageManager,
    ) -> None:
        self.mounts = mounts
        self.nodes = nodes
        self.policies = policies
        self.storage_manager = storage_manager

    async def resolve_owned_roots(self, actor: User, node_ids: list[int]) -> list["Node"]:
        unique_ids = list(dict.fromkeys(node_ids))
        if not unique_ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one node is required")
        if len(unique_ids) > MAX_ARCHIVE_ROOTS:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Too many nodes selected")
        roots = await self.nodes.list_by_ids(unique_ids)
        roots_by_id = {node.id: node for node in roots}
        if len(roots) != len(unique_ids) or any(
            node.owner_id != actor.id or node.deleted_at is not None for node in roots
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        return [roots_by_id[node_id] for node_id in unique_ids]

    async def build_archive_response(
        self,
        roots: list["Node"],
        *,
        filename: str,
    ) -> StreamingResponse:
        if not roots:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one node is required")
        if len(roots) > MAX_ARCHIVE_ROOTS:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Too many nodes selected")

        root_ids = [node.id for node in roots]
        descendant_ids = await self.nodes.collect_descendant_ids(root_ids)
        descendants = [
            node
            for node in await self.nodes.list_by_ids(descendant_ids)
            if node.deleted_at is None
        ]
        node_map = {node.id: node for node in descendants}

        # If callers select both a folder and something inside it, keep only the
        # outer selection so an archive never contains the same file twice.
        selected_ids = set(root_ids)
        effective_roots: list["Node"] = []
        for root in roots:
            cursor = node_map.get(root.parent_id) if root.parent_id is not None else None
            nested = False
            visited: set[int] = set()
            while cursor is not None and cursor.id not in visited:
                if cursor.id in selected_ids:
                    nested = True
                    break
                visited.add(cursor.id)
                cursor = node_map.get(cursor.parent_id) if cursor.parent_id is not None else None
            if not nested:
                effective_roots.append(root)

        mount_cache: dict[int, tuple["BucketMount", "StoragePolicy"]] = {}
        for node in descendants:
            if node.mount_id in mount_cache:
                continue
            mount = await self.mounts.get_by_id(node.mount_id)
            if mount is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
            policy = await self.policies.get_by_id(mount.policy_id)
            if policy is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
            mount_cache[node.mount_id] = (mount, policy)

        root_names: dict[int, str] = {}
        used_root_names: set[str] = set()
        for root in effective_roots:
            root_name = _safe_archive_segment(root.name, f"item-{root.id}")
            root_names[root.id] = _unique_archive_path(
                root_name,
                used_root_names,
                directory=root.type == NodeType.FOLDER,
            ).rstrip("/")

        entries: list[tuple[str, "Node"]] = []
        directory_paths: list[str] = []
        used_paths: set[str] = set()
        for root in effective_roots:
            subtree_ids = await self.nodes.collect_descendant_ids([root.id])
            for node_id in subtree_ids:
                node = node_map.get(node_id)
                if node is None:
                    continue
                parts: list[str] = []
                cursor: "Node | None" = node
                visited: set[int] = set()
                while cursor is not None and cursor.id != root.id and cursor.id not in visited:
                    visited.add(cursor.id)
                    parts.append(_safe_archive_segment(cursor.name, f"item-{cursor.id}"))
                    cursor = node_map.get(cursor.parent_id) if cursor.parent_id is not None else None
                relative_parts = [root_names[root.id], *reversed(parts)]
                archive_path = "/".join(relative_parts)
                unique_path = _unique_archive_path(
                    archive_path,
                    used_paths,
                    directory=node.type == NodeType.FOLDER,
                )
                if node.type == NodeType.FOLDER:
                    directory_paths.append(unique_path)
                elif node.blob_path:
                    entries.append((unique_path, node))

        async def stream_archive():
            temporary_path: str | None = None
            try:
                temporary_path = await asyncio.to_thread(
                    self._write_archive,
                    directory_paths,
                    entries,
                    mount_cache,
                )
                with open(temporary_path, "rb") as archive:
                    while chunk := await asyncio.to_thread(archive.read, ARCHIVE_CHUNK_SIZE):
                        yield chunk
            finally:
                if temporary_path:
                    with contextlib.suppress(FileNotFoundError):
                        await asyncio.to_thread(os.unlink, temporary_path)

        archive_name = filename if filename.lower().endswith(".zip") else f"{filename}.zip"
        return StreamingResponse(
            stream_archive(),
            media_type="application/zip",
            headers={
                "Content-Disposition": build_content_disposition(archive_name),
                "Cache-Control": "private, no-store",
            },
        )

    def _write_archive(
        self,
        directory_paths: list[str],
        entries: list[tuple[str, "Node"]],
        mount_cache: dict[int, tuple["BucketMount", "StoragePolicy"]],
    ) -> str:
        handle = tempfile.NamedTemporaryFile(prefix="cloudrave-download-", suffix=".zip", delete=False)
        temporary_path = handle.name
        handle.close()
        try:
            with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for directory_path in directory_paths:
                    archive.writestr(directory_path, b"")
                for archive_path, node in entries:
                    mount, policy = mount_cache[node.mount_id]
                    adapter = self.storage_manager.get_adapter(policy)
                    source = adapter.open_object(policy, mount, node.blob_path)
                    try:
                        with archive.open(archive_path, "w", force_zip64=True) as target:
                            if hasattr(source, "read"):
                                while chunk := source.read(ARCHIVE_CHUNK_SIZE):
                                    target.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
                            else:
                                for chunk in source:
                                    target.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
                    finally:
                        close = getattr(source, "close", None)
                        if callable(close):
                            close()
            return temporary_path
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)
            raise


class DownloadFileService:
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
        if node is None or node.deleted_at is not None or node.type != NodeType.FILE or not node.blob_path:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        if node.owner_id != actor.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="File access denied")
        return await self._resolve_mount_policy(node)

    async def resolve_shared(self, node_id: int) -> tuple["Node", "BucketMount", "StoragePolicy"]:
        node = await self.nodes.get_by_id(node_id)
        if node is None or node.deleted_at is not None or node.type != NodeType.FILE or not node.blob_path:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File node not found")
        return await self._resolve_mount_policy(node)

    async def _resolve_mount_policy(self, node: "Node") -> tuple["Node", "BucketMount", "StoragePolicy"]:
        mount = await self.mounts.get_by_id(node.mount_id)
        if mount is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
        policy = await self.policies.get_by_id(mount.policy_id)
        if policy is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
        return node, mount, policy

    def build_file_stream(
        self,
        node: "Node",
        mount: "BucketMount",
        policy: "StoragePolicy",
    ) -> FileResponse | StreamingResponse | RedirectResponse:
        adapter = self.storage_manager.get_adapter(policy)
        if policy.provider == StoragePolicyType.LOCAL_FS:
            content_type = mimetypes.guess_type(node.name)[0] or "application/octet-stream"
            stream = adapter.open_object(policy, mount, node.blob_path)
            local_path = getattr(stream, "name", None)
            if isinstance(local_path, (str, os.PathLike)):
                stream.close()
                return FileResponse(
                    path=local_path,
                    media_type=content_type,
                    filename=node.name,
                    headers={"Cache-Control": "private, no-store"},
                )
            return StreamingResponse(
                stream,
                media_type=content_type,
                headers={"Content-Disposition": build_content_disposition(node.name)},
            )
        signed = adapter.build_download_url(policy, mount, node.blob_path)
        return RedirectResponse(signed.url)

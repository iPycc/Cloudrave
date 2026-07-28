"""Archive listing and entry preview behavior."""

from __future__ import annotations

import asyncio
import mimetypes
import tarfile
import zipfile
from pathlib import Path

from fastapi import HTTPException, status

from models.tables import BucketMount, Node, StoragePolicy, User
from services.explorer.view.base import ARCHIVE_EXTENSIONS, preview_kind


class ArcMixin:
    async def archive_entry(self, actor: User, node_id: int, entry_path: str) -> tuple[bytes, str, str]:
        node, mount, policy = await self.resolver.resolve(actor, node_id)
        return await self._archive_entry(node, mount, policy, entry_path)

    async def shared_archive_entry(self, node_id: int, entry_path: str) -> tuple[bytes, str, str]:
        node, mount, policy = await self.resolver.resolve_shared(node_id)
        return await self._archive_entry(node, mount, policy, entry_path)

    async def _archive_entry(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        entry_path: str,
    ) -> tuple[bytes, str, str]:
        if preview_kind(node.name) != "archive":
            raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="File is not an archive")
        normalized = entry_path.replace("\\", "/").lstrip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid archive entry path")
        try:
            payload = await asyncio.to_thread(self._read_archive_entry, node, mount, policy, normalized)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archive entry not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
        return payload, Path(normalized).name, mimetypes.guess_type(normalized)[0] or "application/octet-stream"
    def _archive_entries(self, node: Node, mount: BucketMount, policy: StoragePolicy) -> list[dict[str, object]]:
        extension = Path(node.name).suffix.lower().lstrip(".")
        if extension not in ARCHIVE_EXTENSIONS:
            raise OSError("This archive format requires downloading the original file")
        entries: list[dict[str, object]] = []
        with self._open_seekable(node, mount, policy) as stream:
            if extension == "zip":
                with zipfile.ZipFile(stream) as archive:
                    for item in archive.infolist()[:200]:
                        entries.append({"name": item.filename, "size": item.file_size, "directory": item.is_dir()})
            else:
                with tarfile.open(fileobj=stream, mode="r:*") as archive:
                    for index, item in enumerate(archive):
                        if index >= 200:
                            break
                        entries.append({"name": item.name, "size": item.size, "directory": item.isdir()})
        return entries

    def _read_archive_entry(
        self,
        node: Node,
        mount: BucketMount,
        policy: StoragePolicy,
        entry_path: str,
    ) -> bytes:
        max_entry_bytes = 32 * 1024 * 1024
        extension = Path(node.name).suffix.lower().lstrip(".")
        with self._open_seekable(node, mount, policy) as stream:
            if extension == "zip":
                with zipfile.ZipFile(stream) as archive:
                    info = archive.getinfo(entry_path)
                    if info.is_dir():
                        raise KeyError(entry_path)
                    if info.file_size > max_entry_bytes:
                        raise ValueError("Archive entry is too large for online preview")
                    if info.compress_size and info.file_size / info.compress_size > 200:
                        raise ValueError("Archive entry compression ratio is unsafe")
                    return archive.read(info)
            with tarfile.open(fileobj=stream, mode="r:*") as archive:
                info = archive.getmember(entry_path)
                if not info.isfile():
                    raise KeyError(entry_path)
                if info.size > max_entry_bytes:
                    raise ValueError("Archive entry is too large for online preview")
                extracted = archive.extractfile(info)
                if extracted is None:
                    raise KeyError(entry_path)
                return extracted.read(max_entry_bytes + 1)

"""Streaming ZIP downloads for files and virtual folders."""

from __future__ import annotations

import asyncio
import io
import queue
import threading
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Callable

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse

from models.enums import NodeType
from models.tables import User
from repositories.mounts import BucketMountRepository
from repositories.nodes import NodeRepository
from repositories.policies import StoragePolicyRepository
from services.explorer.source import content_disposition, download_probe_response
from store.manager import StorageManager

if TYPE_CHECKING:
    from models.tables import BucketMount, Node, StoragePolicy


CHUNK_SIZE = 1024 * 1024
MAX_ROOTS = 100
MAX_NODES = 100_000
QUEUE_DEPTH = 8
MAX_CONCURRENT_ARCHIVES = 4
_END = object()
_ARCHIVE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_ARCHIVES)


@dataclass(frozen=True, slots=True)
class _ArchiveError:
    error: Exception


def _safe_name(value: str, fallback: str = "item") -> str:
    segment = value.replace("\\", "_").replace("/", "_").replace("\x00", "")
    segment = segment.replace("\r", " ").replace("\n", " ").strip()
    return fallback if segment in {"", ".", ".."} else segment


def _unique_path(path: str, used: set[str], *, directory: bool) -> str:
    normalized = str(PurePosixPath(path)).rstrip("/")
    candidate = normalized
    counter = 2
    while candidate.casefold() in used:
        parent, separator, name = normalized.rpartition("/")
        if directory:
            next_name = f"{name} ({counter})"
        else:
            stem, dot, extension = name.rpartition(".")
            next_name = f"{stem} ({counter}).{extension}" if dot and stem else f"{name} ({counter})"
        candidate = f"{parent}{separator}{next_name}"
        counter += 1
    used.add(candidate.casefold())
    return f"{candidate}/" if directory else candidate


class _QueueWriter(io.RawIOBase):
    """A non-seekable ZIP target with bounded producer backpressure."""

    def __init__(
        self,
        chunks: queue.Queue[bytes | _ArchiveError | object],
        stopped: threading.Event,
        notify: Callable[[], None],
    ) -> None:
        super().__init__()
        self.chunks = chunks
        self.stopped = stopped
        self.notify = notify
        self.position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.position

    def write(self, data: bytes | bytearray | memoryview) -> int:
        payload = bytes(data)
        if not payload:
            return 0
        while not self.stopped.is_set():
            try:
                self.chunks.put(payload, timeout=0.25)
                self.position += len(payload)
                self.notify()
                return len(payload)
            except queue.Full:
                continue
        raise BrokenPipeError("archive consumer disconnected")


async def _acquire_archive_slot() -> None:
    """Wait for a producer slot without blocking the event-loop thread."""

    while not _ARCHIVE_SLOTS.acquire(blocking=False):
        await asyncio.sleep(0.05)


class ArchiveService:
    """Build a ZIP incrementally without storing the archive in RAM or on disk."""

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
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="At least one node is required")
        if len(unique_ids) > MAX_ROOTS:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Too many nodes selected")
        roots = await self.nodes.list_by_ids(unique_ids)
        roots_by_id = {node.id: node for node in roots}
        if len(roots) != len(unique_ids) or any(
            node.owner_id != actor.id or node.deleted_at is not None for node in roots
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")
        return [roots_by_id[node_id] for node_id in unique_ids]

    async def response(self, roots: list["Node"], *, filename: str) -> StreamingResponse:
        directories, entries, mount_cache = await self._entries(roots)

        async def stream():
            chunks: queue.Queue[bytes | _ArchiveError | object] = queue.Queue(maxsize=QUEUE_DEPTH)
            stopped = threading.Event()
            ready = asyncio.Event()
            loop = asyncio.get_running_loop()

            def notify() -> None:
                try:
                    loop.call_soon_threadsafe(ready.set)
                except RuntimeError:
                    stopped.set()

            await _acquire_archive_slot()
            producer = threading.Thread(
                target=self._write,
                args=(chunks, stopped, notify, directories, entries, mount_cache),
                name="cr-zip",
                daemon=True,
            )
            try:
                producer.start()
                while True:
                    try:
                        chunk = chunks.get_nowait()
                    except queue.Empty:
                        ready.clear()
                        try:
                            chunk = chunks.get_nowait()
                        except queue.Empty:
                            await ready.wait()
                            continue
                    if chunk is _END:
                        break
                    if isinstance(chunk, _ArchiveError):
                        raise chunk.error
                    yield chunk
            finally:
                stopped.set()
                ready.set()
                if producer.ident is None:
                    _ARCHIVE_SLOTS.release()

        archive_name = filename if filename.lower().endswith(".zip") else f"{filename}.zip"
        return StreamingResponse(
            stream(),
            media_type="application/zip",
            headers={
                "Content-Disposition": content_disposition(archive_name),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def probe_response(self, roots: list["Node"], *, filename: str):
        """Use native browser downloads when an archive reads an operator mount."""

        mode = "cloudrave"
        for mount_id in {node.mount_id for node in roots}:
            mount = await self.mounts.get_by_id(mount_id)
            if mount is None or not getattr(mount, "is_enabled", True):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
            policy = await self.policies.get_by_id(mount.policy_id)
            if policy is None or getattr(policy, "status", "active") != "active":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
            adapter = self.storage_manager.get_adapter(policy)
            if getattr(adapter.capabilities, "download_transport", "cloudrave") == "browser":
                mode = "browser"
                break
        archive_name = filename if filename.lower().endswith(".zip") else f"{filename}.zip"
        return download_probe_response(archive_name, mode)

    async def build_archive_response(self, roots: list["Node"], *, filename: str) -> StreamingResponse:
        """Compatibility name used by the current explorer and share services."""

        return await self.response(roots, filename=filename)

    async def _entries(
        self,
        roots: list["Node"],
    ) -> tuple[
        list[str],
        list[tuple[str, "Node"]],
        dict[int, tuple["BucketMount", "StoragePolicy"]],
    ]:
        if not roots:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="At least one node is required")
        if len(roots) > MAX_ROOTS:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Too many nodes selected")

        root_ids = [node.id for node in roots]
        descendant_ids = await self.nodes.collect_descendant_ids(root_ids)
        if len(descendant_ids) > MAX_NODES:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Archive contains too many nodes")
        descendants = [node for node in await self.nodes.list_by_ids(descendant_ids) if node.deleted_at is None]
        node_map = {node.id: node for node in descendants}

        selected_ids = set(root_ids)
        effective_roots: list["Node"] = []
        for root in roots:
            cursor = node_map.get(root.parent_id) if root.parent_id is not None else None
            visited: set[int] = set()
            while cursor is not None and cursor.id not in visited and cursor.id not in selected_ids:
                visited.add(cursor.id)
                cursor = node_map.get(cursor.parent_id) if cursor.parent_id is not None else None
            if cursor is None or cursor.id not in selected_ids:
                effective_roots.append(root)

        mount_cache: dict[int, tuple["BucketMount", "StoragePolicy"]] = {}
        for node in descendants:
            if node.mount_id in mount_cache:
                continue
            mount = await self.mounts.get_by_id(node.mount_id)
            if mount is None or not getattr(mount, "is_enabled", True):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bucket mount not found")
            policy = await self.policies.get_by_id(mount.policy_id)
            if policy is None or getattr(policy, "status", "active") != "active":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage policy not found")
            mount_cache[node.mount_id] = (mount, policy)

        root_names: dict[int, str] = {}
        used_roots: set[str] = set()
        for root in effective_roots:
            root_names[root.id] = _unique_path(
                _safe_name(root.name, f"item-{root.id}"),
                used_roots,
                directory=root.type == NodeType.FOLDER,
            ).rstrip("/")

        entries: list[tuple[str, "Node"]] = []
        directories: list[str] = []
        used_paths: set[str] = set()
        for root in effective_roots:
            for node_id in await self.nodes.collect_descendant_ids([root.id]):
                node = node_map.get(node_id)
                if node is None:
                    continue
                parts: list[str] = []
                cursor: "Node | None" = node
                visited: set[int] = set()
                while cursor is not None and cursor.id != root.id and cursor.id not in visited:
                    visited.add(cursor.id)
                    parts.append(_safe_name(cursor.name, f"item-{cursor.id}"))
                    cursor = node_map.get(cursor.parent_id) if cursor.parent_id is not None else None
                path = "/".join([root_names[root.id], *reversed(parts)])
                path = _unique_path(path, used_paths, directory=node.type == NodeType.FOLDER)
                if node.type == NodeType.FOLDER:
                    directories.append(path)
                elif node.blob_path:
                    entries.append((path, node))
        return directories, entries, mount_cache

    def _write(
        self,
        chunks: queue.Queue[bytes | _ArchiveError | object],
        stopped: threading.Event,
        notify: Callable[[], None],
        directories: list[str],
        entries: list[tuple[str, "Node"]],
        mount_cache: dict[int, tuple["BucketMount", "StoragePolicy"]],
    ) -> None:
        writer = _QueueWriter(chunks, stopped, notify)

        def signal(item: _ArchiveError | object) -> None:
            while not stopped.is_set():
                try:
                    chunks.put(item, timeout=0.25)
                    notify()
                    return
                except queue.Full:
                    continue

        try:
            with zipfile.ZipFile(writer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for path in directories:
                    if stopped.is_set():
                        raise BrokenPipeError("archive consumer disconnected")
                    archive.writestr(path, b"")
                for path, node in entries:
                    if stopped.is_set():
                        raise BrokenPipeError("archive consumer disconnected")
                    mount, policy = mount_cache[node.mount_id]
                    adapter = self.storage_manager.get_adapter(policy)
                    source = adapter.open_object(policy, mount, node.blob_path)
                    try:
                        with archive.open(path, "w", force_zip64=True) as target:
                            while not stopped.is_set() and (chunk := source.read(CHUNK_SIZE)):
                                target.write(chunk.encode() if isinstance(chunk, str) else chunk)
                            if stopped.is_set():
                                raise BrokenPipeError("archive consumer disconnected")
                    finally:
                        close = getattr(source, "close", None)
                        if callable(close):
                            close()
        except BrokenPipeError:
            pass
        except Exception as exc:
            signal(_ArchiveError(exc))
        finally:
            signal(_END)
            _ARCHIVE_SLOTS.release()


ArchiveDownloadService = ArchiveService

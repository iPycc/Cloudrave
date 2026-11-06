"""Bounded ZIP extraction jobs exposed in the background-task page."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import mimetypes
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from threading import Event
from uuid import uuid4

from fastapi import HTTPException, status
import py7zr

from models.enums import NodeType, UserStatus
from security.capabilities import Capability, require_capability
from services.explorer.write_policy import StorageWritePolicy
from services.explorer.archive_limits import BoundedArchiveFactory
from repositories.nodes import _unique_name
from schemas.task import BackgroundTaskRead
from store.call import call_adapter
from store.key import managed_key


MAX_ENTRIES = 10_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRY_BYTES = 128 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
CHUNK_SIZE = 1024 * 1024
MAX_RUNNING_TASKS = 4
MAX_USER_TASKS = 1
MAX_TASK_HISTORY = 200
MAX_USER_HISTORY = 50
TASK_TIMEOUT_SECONDS = 300

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _Task:
    id: str
    owner_id: int
    name: str
    status: str
    progress: int
    detail: str | None
    result_node_id: int | None
    created_at: datetime
    updated_at: datetime
    deadline: float = 0.0


@dataclass(slots=True, frozen=True)
class _ExtractSource:
    node_name: str
    blob_path: str
    mount: Any
    policy: Any
    parent_id: int


_tasks: dict[str, _Task] = {}
_running: set[asyncio.Task[None]] = set()


def _read(task: _Task) -> BackgroundTaskRead:
    return BackgroundTaskRead(
        id=task.id,
        owner_id=task.owner_id,
        kind="archive_extract",
        name=task.name,
        status=task.status,
        progress=task.progress,
        detail=task.detail,
        result_node_id=task.result_node_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def list_tasks(owner_id: int) -> list[BackgroundTaskRead]:
    _trim_history()
    return [
        _read(task)
        for task in sorted(_tasks.values(), key=lambda item: item.created_at, reverse=True)
        if task.owner_id == owner_id
    ]


async def start_extract(
    dep,
    actor_id: int,
    node_id: int,
    target_parent_id: int | None,
    selected_paths: list[str] | None = None,
) -> BackgroundTaskRead:
    async with dep.session_scope() as session:
        actor = await dep.get_user_repository(session).get_by_id(actor_id)
        _require_actor(actor)
        node = await dep.get_node_repository(session).get_by_id(node_id)
        if (
            node is None
            or node.owner_id != actor_id
            or node.type != NodeType.FILE
            or node.deleted_at is not None
            or Path(node.name).suffix.lower() not in {".zip", ".7z"}
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archive file not found")
        await StorageWritePolicy(session).check(actor, node.mount_id)
        if target_parent_id is not None:
            parent = await dep.get_node_repository(session).get_by_id(target_parent_id)
            if (
                parent is None
                or parent.owner_id != actor_id
                or parent.mount_id != node.mount_id
                or parent.type != NodeType.FOLDER
                or parent.deleted_at is not None
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target folder not found")
        if selected_paths:
            if any(_safe_parts(path) is None for path in selected_paths):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid archive entry path")
            selected_paths = list(dict.fromkeys(path.replace("\\", "/").strip("/") for path in selected_paths))

    now = datetime.now(UTC)
    active = [task for task in _tasks.values() if task.status in {"pending", "running"}]
    if len(active) >= MAX_RUNNING_TASKS or sum(task.owner_id == actor_id for task in active) >= MAX_USER_TASKS:
        raise HTTPException(status_code=429, detail="Archive extraction limit reached", headers={"Retry-After": "30"})
    _trim_history()
    task = _Task(
        id=str(uuid4()),
        owner_id=actor_id,
        name=f"解压 {node.name}",
        status="pending",
        progress=0,
        detail="等待后台任务",
        result_node_id=None,
        created_at=now,
        updated_at=now,
        deadline=time.monotonic() + TASK_TIMEOUT_SECONDS,
    )
    _tasks[task.id] = task
    runner = asyncio.create_task(
        _run_extract(dep, task, node_id, target_parent_id, selected_paths),
        name=f"archive-extract-{task.id}",
    )
    _running.add(runner)
    def finished(runner):
        _running.discard(runner)
        if task.status in {"pending", "running"}:
            _update(task, status="failed", detail="任务已取消")
        _trim_history()
    runner.add_done_callback(finished)
    return _read(task)


def _update(task: _Task, **values) -> None:
    for key, value in values.items():
        setattr(task, key, value)
    task.updated_at = datetime.now(UTC)


def _trim_history() -> None:
    completed = sorted((task for task in _tasks.values() if task.status in {"completed", "failed"}),
                       key=lambda task: task.updated_at, reverse=True)
    counts: dict[int, int] = {}
    retained = 0
    for task in completed:
        counts[task.owner_id] = counts.get(task.owner_id, 0) + 1
        if counts[task.owner_id] > MAX_USER_HISTORY or retained >= MAX_TASK_HISTORY:
            _tasks.pop(task.id, None)
        else:
            retained += 1


def _check_deadline(task) -> None:
    if getattr(task, "deadline", 0) and time.monotonic() > task.deadline:
        raise TimeoutError("Archive extraction timed out")


def _require_actor(actor) -> None:
    if actor is None or actor.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Account is unavailable")
    require_capability(actor, Capability.TASK_MANAGE)


async def _write_context(dep, session, owner_id: int, mount_id: int, size: int = 0):
    actor = await dep.get_user_repository(session).get_by_id(owner_id)
    _require_actor(actor)
    return await StorageWritePolicy(session).check(actor, mount_id, size)


async def _check_archive_budget(dep, task: _Task, source: _ExtractSource, size: int) -> None:
    async with dep.session_scope() as session:
        await _write_context(dep, session, task.owner_id, source.mount.id, size)


def _safe_parts(filename: str) -> tuple[str, ...] | None:
    normalized = filename.replace("\\", "/").lstrip("/")
    parts = tuple(part for part in PurePosixPath(normalized).parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


def _is_selected(filename: str, selected_paths: list[str] | None) -> bool:
    if not selected_paths:
        return True
    normalized = filename.replace("\\", "/").strip("/")
    return any(normalized == path or normalized.startswith(f"{path}/") for path in selected_paths)


async def _call_stream_method(method: Callable[..., Any], *args: Any) -> Any:
    if inspect.iscoroutinefunction(method):
        return await method(*args)
    result = await asyncio.to_thread(method, *args)
    return await result if inspect.isawaitable(result) else result


async def _copy_stream(source: Any, target: BinaryIO, task=None) -> None:
    copied = 0
    while chunk := await _call_stream_method(source.read, CHUNK_SIZE):
        if task is not None:
            _check_deadline(task)
        copied += len(chunk)
        if copied > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive source is too large")
        await asyncio.to_thread(target.write, chunk)


async def _close_stream(source: Any) -> None:
    close = getattr(source, "close", None)
    if close is not None:
        await _call_stream_method(close)


async def _load_extract_source(dep, task: _Task, node_id: int, target_parent_id: int | None) -> _ExtractSource:
    """Load archive metadata without keeping a transaction over storage I/O."""

    _check_deadline(task)
    async with dep.session_scope() as session:
        nodes = dep.get_node_repository(session)
        node = await nodes.get_by_id(node_id)
        if node is None or node.owner_id != task.owner_id or node.deleted_at is not None or not node.blob_path:
            raise RuntimeError("ZIP source no longer exists")
        if node.size > MAX_ARCHIVE_BYTES:
            raise HTTPException(status_code=413, detail="Archive source exceeds 512 MB")
        mount, policy = await _write_context(dep, session, task.owner_id, node.mount_id)
        parent_id = target_parent_id if target_parent_id is not None else node.parent_id
        if parent_id is not None:
            parent = await nodes.get_by_id(parent_id)
            if (parent is None or parent.owner_id != task.owner_id or parent.mount_id != mount.id
                    or parent.type != NodeType.FOLDER or parent.deleted_at is not None):
                raise HTTPException(status_code=404, detail="Target folder not found")
        if parent_id is None:
            parent = await nodes.ensure_mount_root(owner_id=task.owner_id, mount_id=mount.id, name=mount.name)
            parent_id = parent.id
        return _ExtractSource(
            node_name=node.name,
            blob_path=node.blob_path,
            mount=mount,
            policy=policy,
            parent_id=parent_id,
        )


async def _create_extract_root(dep, task: _Task, source: _ExtractSource) -> int:
    _check_deadline(task)
    async with dep.session_scope() as session:
        await _write_context(dep, session, task.owner_id, source.mount.id)
        nodes = dep.get_node_repository(session)
        existing = await nodes.get_child_names(source.mount.id, source.parent_id)
        root = await nodes.create_folder(
            owner_id=task.owner_id,
            mount_id=source.mount.id,
            parent_id=source.parent_id,
            name=_unique_name(Path(source.node_name).stem, existing),
        )
        return root.id


async def _ensure_folder_path(
    dep,
    task: _Task,
    *,
    mount_id: int,
    folder_ids: dict[tuple[str, ...], int],
    parts: tuple[str, ...],
) -> int:
    cursor: tuple[str, ...] = ()
    for segment in parts:
        _check_deadline(task)
        next_cursor = (*cursor, segment)
        if next_cursor not in folder_ids:
            async with dep.session_scope() as session:
                await _write_context(dep, session, task.owner_id, mount_id)
                nodes = dep.get_node_repository(session)
                parent_id = folder_ids[cursor]
                siblings = await nodes.get_child_names(mount_id, parent_id)
                folder = await nodes.create_folder(
                    owner_id=task.owner_id,
                    mount_id=mount_id,
                    parent_id=parent_id,
                    name=_unique_name(segment, siblings),
                )
                folder_ids[next_cursor] = folder.id
        cursor = next_cursor
    return folder_ids[cursor]


async def _persist_uploaded_file(
    dep,
    task: _Task,
    *,
    source: _ExtractSource,
    parent_id: int,
    filename: str,
    object_key: str,
    size: int,
    checksum: str,
    content_type: str,
    payload: BinaryIO,
) -> str:
    """Check quota and store bytes/metadata while holding the same mount lock."""
    object_written = False
    adapter = None
    _check_deadline(task)
    try:
        async with dep.session_scope() as session:
            mount, policy = await _write_context(dep, session, task.owner_id, source.mount.id, size)
            nodes = dep.get_node_repository(session)
            parent = await nodes.get_by_id(parent_id)
            if (parent is None or parent.owner_id != task.owner_id or parent.mount_id != mount.id
                    or parent.type != NodeType.FOLDER or parent.deleted_at is not None):
                raise HTTPException(status_code=404, detail="Target folder not found")
            adapter = dep.storage_manager.get_adapter(policy)
            stored = await call_adapter(adapter, "put_object", policy, mount, object_key, payload,
                mime_type=content_type, metadata={"origin": "archive_extract"})
            object_written = True
            siblings = await nodes.get_child_names(mount.id, parent_id)
            target_name = _unique_name(filename, siblings)
            created = await nodes.create_file(owner_id=task.owner_id, mount_id=mount.id,
                parent_id=parent_id, name=target_name, blob_path=object_key, size=size)
            await dep.get_object_repository(session).create_or_update(
                node_id=created.id, policy_id=policy.id, blob_path=object_key,
                physical_id=adapter.physical_id(policy, mount, object_key), revive_physical=True,
                checksum=checksum, content_type=content_type,
                extra={"origin": "archive_extract", "etag": getattr(stored, "etag", None)})
        return target_name
    except BaseException:
        if object_written:
            await _cleanup_uploaded_object(adapter, source, object_key)
        raise


async def _cleanup_uploaded_object(adapter, source: _ExtractSource, object_key: str) -> None:
    try:
        await call_adapter(
            adapter,
            "delete_object",
            source.policy,
            source.mount,
            object_key,
            hard_delete=True,
        )
    except Exception:
        LOGGER.exception(
            "Failed to clean up an archive object after metadata persistence failed",
            extra={"object_key": object_key},
        )


async def _write_extract_audit(
    dep,
    task: _Task,
    *,
    node_id: int,
    root_id: int,
    written: int,
) -> None:
    async with dep.session_scope() as session:
        await dep.get_audit_repository(session).create(
            actor_id=task.owner_id,
            action="archive.extract",
            target=f"node:{node_id}",
            detail=f"result_node:{root_id};bytes:{written}",
            ip=None,
            user_agent=None,
            request_id=None,
        )


async def _run_extract_7z(
    dep,
    task: _Task,
    *,
    node_id: int,
    source_info: _ExtractSource,
    adapter,
    spool,
    selected_paths: list[str] | None,
) -> None:
    with py7zr.SevenZipFile(spool, mode="r") as archive:
        infos = [
            info for info in archive.list()
            if _safe_parts(info.filename) and _is_selected(info.filename, selected_paths)
        ]
        if len(infos) > MAX_ENTRIES:
            raise ValueError("7Z 条目过多")
        file_infos = [info for info in infos if info.is_file and not info.is_symlink]
        total_bytes = sum(int(info.uncompressed or 0) for info in file_infos)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("7Z 解压后总体积过大")
        for info in file_infos:
            size = int(info.uncompressed or 0)
            compressed = int(info.compressed or 0)
            if size > MAX_ENTRY_BYTES:
                raise ValueError(f"{info.filename} 超过单文件解压限制")
            if compressed and size / compressed > MAX_COMPRESSION_RATIO:
                raise ValueError(f"{info.filename} 压缩率异常")

        await _check_archive_budget(dep, task, source_info, total_bytes)
        root_id = await _create_extract_root(dep, task, source_info)
        task.result_node_id = root_id
        folder_ids: dict[tuple[str, ...], int] = {(): root_id}
        directory_paths = {
            parts
            for info in infos
            if info.is_directory and (parts := _safe_parts(info.filename))
        }
        directory_paths.update(
            parts[:-1]
            for info in file_infos
            if (parts := _safe_parts(info.filename)) and parts[:-1]
        )
        for folder_path in sorted(directory_paths, key=lambda parts: (len(parts), parts)):
            await _ensure_folder_path(
                dep,
                task,
                mount_id=source_info.mount.id,
                folder_ids=folder_ids,
                parts=folder_path,
            )

        written = 0
        with tempfile.TemporaryDirectory(prefix="cloudrave-7z-extract-") as target_dir:
            extraction_root = Path(target_dir).resolve()
            cancelled = Event()
            def check_output():
                if cancelled.is_set():
                    raise TimeoutError("Archive extraction cancelled")
                _check_deadline(task)
            factory = BoundedArchiveFactory(extraction_root, max_total=MAX_TOTAL_BYTES,
                max_entry=MAX_ENTRY_BYTES, max_entries=MAX_ENTRIES, check_deadline=check_output)
            worker = asyncio.create_task(asyncio.to_thread(archive.extract,
                targets=[info.filename for info in file_infos], factory=factory))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled.set()
                try:
                    await asyncio.shield(worker)
                except Exception:
                    pass
                raise
            for index, info in enumerate(file_infos):
                parts = _safe_parts(info.filename)
                if not parts:
                    continue
                folder_parts, filename = parts[:-1], parts[-1]
                payload_path = (extraction_root.joinpath(*parts)).resolve()
                if extraction_root not in payload_path.parents or not payload_path.is_file():
                    raise ValueError(f"{info.filename} 未能安全解压")
                parent_id = await _ensure_folder_path(
                    dep,
                    task,
                    mount_id=source_info.mount.id,
                    folder_ids=folder_ids,
                    parts=folder_parts,
                )
                object_key = managed_key(
                    task.owner_id,
                    filename,
                    style=str((source_info.mount.extra or {}).get("object_key_style", "readable")),
                )
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                digest = hashlib.sha256()
                with payload_path.open("rb") as entry, tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as payload:
                    while chunk := await asyncio.to_thread(entry.read, CHUNK_SIZE):
                        _check_deadline(task)
                        digest.update(chunk)
                        payload.write(chunk)
                    payload.seek(0)
                    await _persist_uploaded_file(dep, task, source=source_info, parent_id=parent_id,
                        filename=filename, object_key=object_key, size=payload_path.stat().st_size,
                        checksum=digest.hexdigest(), content_type=content_type, payload=payload)
                written += int(info.uncompressed or payload_path.stat().st_size)
                _update(
                    task,
                    progress=min(99, round((index + 1) / max(1, len(file_infos)) * 100)),
                    detail=f"已解压 {index + 1} / {len(file_infos)} 个文件",
                )

        await _write_extract_audit(dep, task, node_id=node_id, root_id=root_id, written=written)


async def _run_extract(
    dep,
    task: _Task,
    node_id: int,
    target_parent_id: int | None,
    selected_paths: list[str] | None,
) -> None:
    _update(task, status="running", detail="读取压缩包目录")
    try:
        source_info = await _load_extract_source(dep, task, node_id, target_parent_id)
        adapter = dep.storage_manager.get_adapter(source_info.policy)

        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as spool:
            source_stream = await call_adapter(
                adapter,
                "open_object",
                source_info.policy,
                source_info.mount,
                source_info.blob_path,
            )
            try:
                await _copy_stream(source_stream, spool, task)
            finally:
                await _close_stream(source_stream)
            spool.seek(0)

            if Path(source_info.node_name).suffix.lower() == ".7z":
                await _run_extract_7z(
                    dep,
                    task,
                    node_id=node_id,
                    source_info=source_info,
                    adapter=adapter,
                    spool=spool,
                    selected_paths=selected_paths,
                )
                _update(task, status="completed", progress=100, detail="解压完成")
                return

            with zipfile.ZipFile(spool) as archive:
                infos = [
                    info for info in archive.infolist()
                    if _safe_parts(info.filename) and _is_selected(info.filename, selected_paths)
                ]
                if len(infos) > MAX_ENTRIES:
                    raise ValueError("ZIP 条目过多")
                total_bytes = sum(info.file_size for info in infos if not info.is_dir())
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("ZIP 解压后总体积过大")
                for info in infos:
                    if info.file_size > MAX_ENTRY_BYTES:
                        raise ValueError(f"{info.filename} 超过单文件解压限制")
                    if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                        raise ValueError(f"{info.filename} 压缩率异常")

                await _check_archive_budget(dep, task, source_info, total_bytes)
                root_id = await _create_extract_root(dep, task, source_info)
                task.result_node_id = root_id
                folder_ids: dict[tuple[str, ...], int] = {(): root_id}
                written = 0
                file_infos = [info for info in infos if not info.is_dir()]

                directory_paths = {
                    parts
                    for info in infos
                    if info.is_dir() and (parts := _safe_parts(info.filename))
                }
                directory_paths.update(
                    parts[:-1]
                    for info in file_infos
                    if (parts := _safe_parts(info.filename)) and parts[:-1]
                )
                for folder_path in sorted(directory_paths, key=lambda parts: (len(parts), parts)):
                    await _ensure_folder_path(
                        dep,
                        task,
                        mount_id=source_info.mount.id,
                        folder_ids=folder_ids,
                        parts=folder_path,
                    )

                for index, info in enumerate(file_infos):
                    parts = _safe_parts(info.filename)
                    if not parts:
                        continue
                    folder_parts, filename = parts[:-1], parts[-1]
                    parent_id = await _ensure_folder_path(
                        dep,
                        task,
                        mount_id=source_info.mount.id,
                        folder_ids=folder_ids,
                        parts=folder_parts,
                    )
                    object_key = managed_key(
                        task.owner_id,
                        filename,
                        style=str((source_info.mount.extra or {}).get("object_key_style", "readable")),
                    )
                    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    digest = hashlib.sha256()
                    with archive.open(info) as entry, tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as payload:
                        actual_size = 0
                        while chunk := await asyncio.to_thread(entry.read, CHUNK_SIZE):
                            _check_deadline(task)
                            actual_size += len(chunk)
                            if actual_size > MAX_ENTRY_BYTES or written + actual_size > MAX_TOTAL_BYTES:
                                raise ValueError("Archive output exceeds extraction limits")
                            digest.update(chunk)
                            payload.write(chunk)
                        payload.seek(0)
                        await _persist_uploaded_file(dep, task, source=source_info, parent_id=parent_id,
                            filename=filename, object_key=object_key, size=actual_size,
                            checksum=digest.hexdigest(), content_type=content_type, payload=payload)
                    written += info.file_size
                    _update(
                        task,
                        progress=min(99, round((index + 1) / max(1, len(file_infos)) * 100)),
                        detail=f"已解压 {index + 1} / {len(file_infos)} 个文件",
                    )

                await _write_extract_audit(
                    dep,
                    task,
                    node_id=node_id,
                    root_id=root_id,
                    written=written,
                )
        _update(task, status="completed", progress=100, detail="解压完成")
    except Exception as exc:
        LOGGER.exception(
            "Archive extraction failed",
            extra={"task_id": task.id, "node_id": node_id},
        )
        _update(task, status="failed", detail=str(exc))

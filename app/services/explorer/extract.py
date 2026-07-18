"""Bounded ZIP extraction jobs exposed in the background-task page."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import HTTPException, status

from app.models.enums import NodeType
from app.repositories.nodes import _unique_name
from app.schemas.task import BackgroundTaskRead
from app.storage.call import call_adapter
from app.storage.key import managed_key


MAX_ENTRIES = 10_000
MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
CHUNK_SIZE = 1024 * 1024


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
    return [
        _read(task)
        for task in sorted(_tasks.values(), key=lambda item: item.created_at, reverse=True)
        if task.owner_id == owner_id
    ]


async def start_extract(dep, actor_id: int, node_id: int, target_parent_id: int | None) -> BackgroundTaskRead:
    async with dep.session_scope() as session:
        node = await dep.get_node_repository(session).get_by_id(node_id)
        if (
            node is None
            or node.owner_id != actor_id
            or node.type != NodeType.FILE
            or node.deleted_at is not None
            or Path(node.name).suffix.lower() != ".zip"
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ZIP file not found")
        if target_parent_id is not None:
            parent = await dep.get_node_repository(session).get_by_id(target_parent_id)
            if (
                parent is None
                or parent.owner_id != actor_id
                or parent.mount_id != node.mount_id
                or parent.type != NodeType.FOLDER
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target folder not found")

    now = datetime.now(UTC)
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
    )
    _tasks[task.id] = task
    runner = asyncio.create_task(
        _run_extract(dep, task, node_id, target_parent_id),
        name=f"archive-extract-{task.id}",
    )
    _running.add(runner)
    runner.add_done_callback(_running.discard)
    return _read(task)


def _update(task: _Task, **values) -> None:
    for key, value in values.items():
        setattr(task, key, value)
    task.updated_at = datetime.now(UTC)


def _safe_parts(filename: str) -> tuple[str, ...] | None:
    normalized = filename.replace("\\", "/").lstrip("/")
    parts = tuple(part for part in PurePosixPath(normalized).parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


async def _run_extract(dep, task: _Task, node_id: int, target_parent_id: int | None) -> None:
    _update(task, status="running", detail="读取 ZIP 目录")
    try:
        async with dep.session_scope() as session:
            nodes = dep.get_node_repository(session)
            mounts = dep.get_mount_repository(session)
            policies = dep.get_policy_repository(session)
            objects = dep.get_object_repository(session)
            audits = dep.get_audit_repository(session)
            node = await nodes.get_by_id(node_id)
            if node is None or not node.blob_path:
                raise RuntimeError("ZIP source no longer exists")
            mount = await mounts.get_by_id(node.mount_id)
            policy = await policies.get_by_id(mount.policy_id) if mount else None
            if mount is None or policy is None:
                raise RuntimeError("ZIP storage mount is unavailable")
            parent_id = target_parent_id if target_parent_id is not None else node.parent_id
            if parent_id is None:
                parent = await nodes.ensure_mount_root(owner_id=task.owner_id, mount_id=mount.id, name=mount.name)
                parent_id = parent.id

            adapter = dep.storage_manager.get_adapter(policy)
            source = await call_adapter(adapter, "open_object", policy, mount, node.blob_path)
            spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
            try:
                await asyncio.to_thread(shutil.copyfileobj, source, spool, CHUNK_SIZE)
            finally:
                source.close()
            spool.seek(0)

            with zipfile.ZipFile(spool) as archive:
                infos = [info for info in archive.infolist() if _safe_parts(info.filename)]
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

                existing = await nodes.get_child_names(mount.id, parent_id)
                root_name = _unique_name(Path(node.name).stem, existing)
                root = await nodes.create_folder(
                    owner_id=task.owner_id,
                    mount_id=mount.id,
                    parent_id=parent_id,
                    name=root_name,
                )
                task.result_node_id = root.id
                folder_ids: dict[tuple[str, ...], int] = {(): root.id}
                written = 0
                file_infos = [info for info in infos if not info.is_dir()]

                for index, info in enumerate(file_infos):
                    parts = _safe_parts(info.filename)
                    if not parts:
                        continue
                    folder_parts, filename = parts[:-1], parts[-1]
                    cursor: tuple[str, ...] = ()
                    for segment in folder_parts:
                        next_cursor = (*cursor, segment)
                        if next_cursor not in folder_ids:
                            siblings = await nodes.get_child_names(mount.id, folder_ids[cursor])
                            folder = await nodes.create_folder(
                                owner_id=task.owner_id,
                                mount_id=mount.id,
                                parent_id=folder_ids[cursor],
                                name=_unique_name(segment, siblings),
                            )
                            folder_ids[next_cursor] = folder.id
                        cursor = next_cursor

                    siblings = await nodes.get_child_names(mount.id, folder_ids[cursor])
                    target_name = _unique_name(filename, siblings)
                    object_key = managed_key(
                        task.owner_id,
                        target_name,
                        style=str((mount.extra or {}).get("object_key_style", "readable")),
                    )
                    digest = hashlib.sha256()
                    with archive.open(info) as entry, tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as payload:
                        while chunk := await asyncio.to_thread(entry.read, CHUNK_SIZE):
                            digest.update(chunk)
                            payload.write(chunk)
                        payload.seek(0)
                        stored = await call_adapter(
                            adapter,
                            "put_object",
                            policy,
                            mount,
                            object_key,
                            payload,
                            mime_type=mimetypes.guess_type(target_name)[0] or "application/octet-stream",
                            metadata={"origin": "archive_extract"},
                        )
                    created = await nodes.create_file(
                        owner_id=task.owner_id,
                        mount_id=mount.id,
                        parent_id=folder_ids[cursor],
                        name=target_name,
                        blob_path=object_key,
                        size=info.file_size,
                    )
                    await objects.create_or_update(
                        node_id=created.id,
                        policy_id=policy.id,
                        blob_path=object_key,
                        physical_id=adapter.physical_id(policy, mount, object_key),
                        revive_physical=True,
                        checksum=digest.hexdigest(),
                        content_type=mimetypes.guess_type(target_name)[0] or "application/octet-stream",
                        extra={"origin": "archive_extract", "etag": getattr(stored, "etag", None)},
                    )
                    written += info.file_size
                    _update(
                        task,
                        progress=min(99, round((index + 1) / max(1, len(file_infos)) * 100)),
                        detail=f"已解压 {index + 1} / {len(file_infos)} 个文件",
                    )

                await audits.create(
                    actor_id=task.owner_id,
                    action="archive.extract",
                    target=f"node:{node.id}",
                    detail=f"result_node:{root.id};bytes:{written}",
                    ip=None,
                    user_agent=None,
                    request_id=None,
                )
            spool.close()
        _update(task, status="completed", progress=100, detail="解压完成")
    except Exception as exc:
        _update(task, status="failed", detail=str(exc))

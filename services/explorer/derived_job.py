"""Durable hash and thumbnail work for completed uploads."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select

from models.tables import DerivedTask, Node, User
from services.explorer.view.base import preview_version


def hash_object(adapter, policy, mount, object_key: str) -> str:
    hasher = hashlib.sha256()
    with adapter.open_object(policy, mount, object_key) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class DerivedWorker:
    def __init__(self, dep, *, interval: int = 5) -> None:
        self.dep = dep
        self.interval = interval
        self.tasks: list[asyncio.Task[None]] = []
        self.log = logging.getLogger("cloudrave.derived")

    def start(self) -> None:
        if not self.tasks or all(task.done() for task in self.tasks):
            self.tasks = [asyncio.create_task(self._loop(kind), name=f"derived-{kind}")
                          for kind in ("thumbnail", "hash")]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.tasks = []

    async def _loop(self, kind: str) -> None:
        while True:
            try:
                task_id = await self._claim(kind)
                if task_id is None:
                    await asyncio.sleep(self.interval)
                    continue
                await self._run_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("Derived file task failed")
                await asyncio.sleep(self.interval)

    async def _claim(self, kind: str) -> int | None:
        now = datetime.now(UTC)
        async with self.dep.session_scope() as session:
            task = await session.scalar(select(DerivedTask).where(DerivedTask.kind == kind, or_(
                and_(DerivedTask.state.in_(("pending", "failed")),
                     or_(DerivedTask.next_run_at.is_(None), DerivedTask.next_run_at <= now)),
                and_(DerivedTask.state == "running", DerivedTask.lease_until <= now),
            )).order_by(DerivedTask.id).with_for_update(skip_locked=True).limit(1))
            if task is None:
                return None
            task.state = "running"
            task.attempts += 1
            task.lease_until = now + timedelta(hours=1)
            return task.id

    async def _run_task(self, task_id: int) -> None:
        try:
            async with self.dep.session_scope() as session:
                task = await session.get(DerivedTask, task_id)
                if task is None:
                    return
                node = await session.get(Node, task.node_id)
                if node is None or node.deleted_at is not None or preview_version(node) != task.version:
                    task.state = "done"
                    return
                if task.kind == "thumbnail":
                    from api.v1.explore.dep import preview_service
                    await preview_service(self.dep, session).image_asset(User(id=node.owner_id), node.id, "thumbnail")
                elif task.kind == "hash":
                    record = await self.dep.get_object_repository(session).get_by_node_id(node.id)
                    if record is not None:
                        mount = await self.dep.get_mount_repository(session).get_by_id(node.mount_id)
                        policy = await self.dep.get_policy_repository(session).get_by_id(record.policy_id)
                        if mount is not None and policy is not None:
                            adapter = self.dep.storage_manager.get_adapter(policy)
                            record.checksum = await asyncio.to_thread(hash_object, adapter, policy, mount, record.blob_path)
                task.state = "done"
                task.error = None
                task.lease_until = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.exception("Derived task %s failed", task_id)
            async with self.dep.session_scope() as session:
                task = await session.get(DerivedTask, task_id)
                if task is not None:
                    task.state = "failed"
                    task.error = str(exc)[:500]
                    task.lease_until = None
                    task.next_run_at = datetime.now(UTC) + timedelta(seconds=min(3600, 30 * 2 ** min(task.attempts, 7)))

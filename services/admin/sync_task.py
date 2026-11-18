"""Persistent, bounded worker for bucket synchronization."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import logging
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update

from db.base import session_scope
from models.enums import UserRole, UserStatus
from models.tables import BucketMount, MountSyncTask, User
from schemas.sync import MountSyncRequest
from services.admin.scan import BucketScanner
from services.admin.sync import MountSyncService

LOGGER = logging.getLogger("cloudrave.sync")


def sync_service(dep, session) -> MountSyncService:
    return MountSyncService(
        session=session,
        mounts=dep.get_mount_repository(session),
        policies=dep.get_policy_repository(session),
        nodes=dep.get_node_repository(session),
        objects=dep.get_object_repository(session),
        audits=dep.get_audit_repository(session),
        scanner=BucketScanner(dep.storage_manager),
    )


async def start_sync_task(dep, session, mount: BucketMount, owner_id: int) -> MountSyncTask:
    mounts = dep.get_mount_repository(session)
    mount = await mounts.get_by_id_for_update(mount.id)
    if mount is None or mount.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Bucket mount not found")
    policy = await dep.get_policy_repository(session).get_by_id(mount.policy_id)
    if not mount.is_enabled or mount.mode != "mirror" or policy is None or policy.status != "active":
        raise HTTPException(status_code=409, detail="Storage mount is not available for synchronization")
    if not dep.storage_manager.get_adapter(policy).capabilities.supports_mirror:
        raise HTTPException(status_code=400, detail="Storage drive does not support synchronization")

    task = await session.scalar(
        select(MountSyncTask).where(MountSyncTask.mount_id == mount.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if task is not None and task.status in {"pending", "running"}:
        return task
    state = MountSyncService._state(mount)
    resuming = task is not None and task.status in {"paused", "failed"} and state.get("generation") == task.generation
    if resuming:
        if state.get("mapping") and state["mapping"] != MountSyncService.mapping_signature(policy, mount):
            raise HTTPException(status_code=409, detail="存储配置已更改，请取消旧任务后重新导入")
        state["status"] = "running"
        state["error"] = None
    else:
        state = MountSyncService._new_state()
        state["mapping"] = MountSyncService.mapping_signature(policy, mount)
    await mounts.save_sync_state(mount, state)
    if task is None:
        task = MountSyncTask(mount_id=mount.id, owner_id=owner_id, generation=state["generation"])
        session.add(task)
    elif not resuming:
        task.id = str(uuid4())
        task.generation = state["generation"]
    task.status = "pending"
    task.error = None
    task.lease_token = None
    task.lease_expires_at = None
    await session.flush()
    return task


async def stop_sync_task(dep, session, task: MountSyncTask, owner_id: int, *, cancel: bool) -> MountSyncTask:
    mount = await dep.get_mount_repository(session).get_by_id_for_update(task.mount_id)
    task = await session.scalar(
        select(MountSyncTask).where(MountSyncTask.id == task.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Synchronization task not found")
    if mount is None or mount.owner_id != owner_id or task.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Synchronization task not found")
    if task.status not in {"pending", "running", "paused", "failed"}:
        raise HTTPException(status_code=409, detail="Synchronization task is already finished")
    state = MountSyncService._state(mount)
    if state.get("generation") == task.generation:
        state["status"] = "canceled" if cancel else "paused"
        await dep.get_mount_repository(session).save_sync_state(mount, state)
    task.status = "canceled" if cancel else "paused"
    task.lease_token = None
    task.lease_expires_at = None
    await session.flush()
    return task


class MountSyncWorker:
    def __init__(self, dep, manager=None, *, interval: float = 1.0) -> None:
        self.dep = dep
        self.manager = manager
        self.interval = interval
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop(), name="mount-sync-worker")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def _loop(self) -> None:
        while True:
            tracked = False
            try:
                if self.manager is not None:
                    tracked = self.manager.begin_business_write()
                    if not tracked:
                        await asyncio.sleep(self.interval)
                        continue
                claimed = await self._claim()
                if claimed:
                    await self._run_page(*claimed)
                else:
                    await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Background synchronization worker failed")
                await asyncio.sleep(self.interval)
            finally:
                if tracked:
                    self.manager.end_business_write()

    async def _claim(self) -> tuple[str, str] | None:
        now = datetime.now(UTC)
        eligible = or_(
            MountSyncTask.status == "pending",
            and_(MountSyncTask.status == "running", MountSyncTask.lease_expires_at < now),
        )
        async with session_scope(self.dep.session_factory) as session:
            task_id = await session.scalar(
                select(MountSyncTask.id).where(eligible).order_by(MountSyncTask.updated_at, MountSyncTask.id).limit(1)
            )
            if task_id is None:
                return None
            token = str(uuid4())
            result = await session.execute(
                update(MountSyncTask).where(MountSyncTask.id == task_id, eligible).values(
                    status="running", lease_token=token, lease_expires_at=now + timedelta(minutes=5),
                    updated_at=now,
                )
            )
            return (task_id, token) if result.rowcount == 1 else None

    async def _run_page(self, task_id: str, token: str) -> None:
        request = SimpleNamespace(client=None, headers={}, state=SimpleNamespace(request_id=f"sync-{task_id}"))
        error = None
        try:
            async with session_scope(self.dep.session_factory) as session:
                task = await session.get(MountSyncTask, task_id)
                if task is None or task.lease_token != token or task.status != "running":
                    return
                actor = await session.get(User, task.owner_id)
                if actor is None or actor.status != UserStatus.ACTIVE or actor.role == UserRole.GUEST:
                    raise RuntimeError("账号已停用，无法继续同步")
                mount = await session.get(BucketMount, task.mount_id)
                if mount is None or mount.owner_id != actor.id:
                    raise RuntimeError("存储挂载已失效")
                state = MountSyncService._state(mount)
                complete = state.get("generation") == task.generation and state.get("status") == "complete"
                if not complete:
                    result = await sync_service(self.dep, session).run(
                        request, actor, task.mount_id, MountSyncRequest(page_size=200), generation=task.generation,
                    )
                    complete = result.complete
                    if result.status == "failed":
                        raise RuntimeError("存储同步失败，请检查连接后重试")
        except asyncio.CancelledError:
            raise
        except Exception:
            error = "同步未完成，请检查存储连接或重新授权后重试"
            complete = False
        async with session_scope(self.dep.session_factory) as session:
            values = {
                "status": "failed" if error else "completed" if complete else "pending",
                "error": error, "lease_token": None, "lease_expires_at": None,
                "updated_at": datetime.now(UTC),
            }
            await session.execute(
                update(MountSyncTask)
                .where(MountSyncTask.id == task_id, MountSyncTask.lease_token == token, MountSyncTask.status == "running")
                .values(**values)
            )

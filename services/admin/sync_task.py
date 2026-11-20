"""Persistent, bounded worker for bucket synchronization."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import logging
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import httpx
from sqlalchemy import delete, select, update

from db.base import session_scope
from models.enums import UserRole, UserStatus
from models.tables import BucketMount, MountSyncConflict, MountSyncTask, User
from schemas.sync import MountSyncRequest
from services.admin.scan import BucketScanner
from services.admin.sync import MountSyncService
from services.admin.task_claim import claim_task, eligible
from store.errors import storage_error_code, unwrap_storage_error

LOGGER = logging.getLogger("cloudrave.sync")
MAX_RETRIES = 3
LEASE_DURATION = timedelta(minutes=5)
LEASE_RENEW_INTERVAL = 60


def _is_transient(exc: Exception) -> bool:
    cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
    cause = unwrap_storage_error(cause)
    if isinstance(cause, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError)):
        return True
    if storage_error_code(cause) in {"SlowDown", "RequestTimeout", "ServiceUnavailable", "InternalError"}:
        return True
    get_status = getattr(cause, "get_status_code", None)
    status = get_status() if callable(get_status) else getattr(cause, "status_code", None)
    if status is None:
        status = getattr(getattr(cause, "response", None), "status_code", None)
    try:
        return int(status) == 429 or 500 <= int(status) <= 599
    except (TypeError, ValueError):
        return False


def sync_service(dep, session, *, executor: ThreadPoolExecutor | None = None) -> MountSyncService:
    return MountSyncService(
        session=session,
        mounts=dep.get_mount_repository(session),
        policies=dep.get_policy_repository(session),
        nodes=dep.get_node_repository(session),
        objects=dep.get_object_repository(session),
        audits=dep.get_audit_repository(session),
        scanner=BucketScanner(dep.storage_manager, executor=executor),
    )


async def start_sync_task(
    dep, session, mount: BucketMount, owner_id: int, *, resume_task_id: str | None = None,
) -> MountSyncTask:
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
    state = MountSyncService._state(mount)
    if resume_task_id is not None:
        if task is None or task.id != resume_task_id or task.owner_id != owner_id:
            raise HTTPException(status_code=404, detail="Synchronization task not found")
        if (
            task.status not in {"pending", "running", "paused", "failed"}
            or state.get("generation") != task.generation
        ):
            raise HTTPException(status_code=409, detail="此导入任务无法继续，请重新发起导入")
    if task is not None and task.status in {"pending", "running"}:
        return task
    resuming = task is not None and task.status in {"paused", "failed"} and state.get("generation") == task.generation
    if resuming:
        if state.get("mapping") and state["mapping"] != MountSyncService.mapping_signature(policy, mount):
            raise HTTPException(status_code=409, detail="存储配置已更改，请取消旧任务后重新导入")
        state["status"] = "running"
        state["error"] = None
        state["retry_count"] = 0
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
        self._storage_executor: ThreadPoolExecutor | None = None
        self._next_conflict_prune = 0.0

    def start(self) -> None:
        if self.task is None or self.task.done():
            if self._storage_executor is None:
                self._storage_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloudrave-import")
            self.task = asyncio.create_task(self._loop(), name="mount-sync-worker")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        if self._storage_executor is not None:
            self._storage_executor.shutdown(wait=False, cancel_futures=True)
            self._storage_executor = None

    async def _loop(self) -> None:
        while True:
            tracked = False
            should_sleep = False
            try:
                if self.manager is not None:
                    tracked = self.manager.begin_business_write()
                    if not tracked:
                        should_sleep = True
                if not should_sleep:
                    claimed = await self._claim()
                    if claimed:
                        heartbeat = asyncio.create_task(self._renew_lease(*claimed), name="mount-sync-lease")
                        try:
                            await self._run_page(*claimed)
                        except asyncio.CancelledError:
                            try:
                                await self._finish_page(*claimed, status="pending")
                            except Exception:
                                LOGGER.exception("Could not release synchronization task lease at shutdown")
                            raise
                        finally:
                            heartbeat.cancel()
                            with suppress(asyncio.CancelledError):
                                await heartbeat
                    else:
                        now = time.monotonic()
                        if now >= self._next_conflict_prune:
                            self._next_conflict_prune = now + 60
                            if await self._prune_obsolete_conflicts():
                                self._next_conflict_prune = now + self.interval
                        should_sleep = True
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Background synchronization worker failed")
                should_sleep = True
            finally:
                if tracked:
                    self.manager.end_business_write()
            if should_sleep:
                await asyncio.sleep(self.interval)

    async def _prune_obsolete_conflicts(self) -> int:
        # A mount keeps one visible task. Older generations are inaccessible once
        # a new task starts, so remove them in short batches while the worker is idle.
        async with session_scope(self.dep.session_factory) as session:
            ids = list(await session.scalars(
                select(MountSyncConflict.id)
                .join(MountSyncTask, MountSyncTask.mount_id == MountSyncConflict.mount_id)
                .where(MountSyncConflict.generation != MountSyncTask.generation)
                .order_by(MountSyncConflict.id)
                .limit(200)
            ))
            if ids:
                await session.execute(delete(MountSyncConflict).where(MountSyncConflict.id.in_(ids)))
            return len(ids)

    async def _claim(self) -> tuple[str, str] | None:
        return await claim_task(self.dep.session_factory, MountSyncTask)

    @staticmethod
    def _eligible(now: datetime):
        return eligible(MountSyncTask, now)

    async def _renew_lease(self, task_id: str, token: str) -> None:
        while True:
            await asyncio.sleep(LEASE_RENEW_INTERVAL)
            try:
                now = datetime.now(UTC)
                async with session_scope(self.dep.session_factory) as session:
                    result = await session.execute(
                        update(MountSyncTask).where(
                            MountSyncTask.id == task_id,
                            MountSyncTask.status == "running",
                            MountSyncTask.lease_token == token,
                            MountSyncTask.lease_expires_at > now,
                        ).values(lease_expires_at=now + LEASE_DURATION)
                    )
                    if result.rowcount != 1:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient database error may recover before the lease expires.
                LOGGER.exception("Could not renew synchronization task lease")

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
                    result = await sync_service(self.dep, session, executor=self._storage_executor).run(
                        request, actor, task.mount_id, MountSyncRequest(page_size=200),
                        generation=task.generation, lease=(task_id, token),
                    )
                    complete = result.complete
                    if result.status == "failed":
                        raise RuntimeError("存储同步失败，请检查连接后重试")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_transient(exc) and await self._schedule_retry(task_id, token):
                return
            error = "同步未完成，请检查存储连接或重新授权后重试"
            complete = False
        page_status = "failed" if error else "completed" if complete else "pending"
        await self._finish_page(task_id, token, status=page_status, error=error)

    async def _finish_page(self, task_id: str, token: str, *, status: str, error: str | None = None) -> None:
        async with session_scope(self.dep.session_factory) as session:
            values = {
                "status": status,
                "error": error, "lease_token": None, "lease_expires_at": None,
                "updated_at": datetime.now(UTC),
            }
            await session.execute(
                update(MountSyncTask)
                .where(MountSyncTask.id == task_id, MountSyncTask.lease_token == token, MountSyncTask.status == "running")
                .values(**values)
            )

    async def _schedule_retry(self, task_id: str, token: str) -> bool:
        async with session_scope(self.dep.session_factory) as session:
            task = await session.get(MountSyncTask, task_id)
            if task is None:
                return False
            mount = await self.dep.get_mount_repository(session).get_by_id_for_update(task.mount_id)
            task = await session.scalar(
                select(MountSyncTask).where(MountSyncTask.id == task_id)
                .with_for_update().execution_options(populate_existing=True)
            )
            if mount is None or task is None or task.lease_token != token or task.status != "running":
                return False
            state = MountSyncService._state(mount)
            attempts = int(state.get("retry_count", 0))
            if state.get("generation") != task.generation or state.get("status") not in {"running", "error"}:
                return False
            if attempts >= MAX_RETRIES:
                return False
            state["status"] = "running"
            state["error"] = None
            state["retry_count"] = attempts + 1
            await self.dep.get_mount_repository(session).save_sync_state(mount, state)
            task.status = "pending"
            task.error = "存储服务暂时不可用，正在重试"
            task.lease_token = None
            task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=2 ** (attempts + 1))
            await session.flush()
            return True

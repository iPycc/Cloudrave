"""Persistence for durable object cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gc import GcTask


class GcRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, policy_id: int, mount_id: int, blob_path: str, physical_id: str) -> GcTask:
        task = GcTask(
            policy_id=policy_id,
            mount_id=mount_id,
            blob_path=blob_path,
            physical_id=physical_id,
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def claim_by_ids(self, task_ids: list[int]) -> list[GcTask]:
        if not task_ids:
            return []
        return await self._claim(select(GcTask).where(GcTask.id.in_(task_ids), GcTask.state == "pending"))

    async def claim_pending(self, limit: int = 100) -> list[GcTask]:
        now = datetime.now(UTC)
        return await self._claim(
            select(GcTask)
            .where(
                GcTask.state == "pending",
                or_(GcTask.next_attempt_at.is_(None), GcTask.next_attempt_at <= now),
            )
            .order_by(GcTask.id.asc())
            .limit(limit)
        )

    async def _claim(self, statement) -> list[GcTask]:
        rows = await self.session.scalars(statement.with_for_update(skip_locked=True))
        tasks = list(rows)
        for task in tasks:
            task.state = "processing"
        await self.session.flush()
        return tasks

    async def done(self, task: GcTask) -> None:
        task.state = "done"
        task.error = None
        task.attempts += 1
        task.next_attempt_at = None
        await self.session.flush()

    async def skipped(self, task: GcTask) -> None:
        task.state = "skipped"
        task.error = None
        task.attempts += 1
        task.next_attempt_at = None
        await self.session.flush()

    async def retry(self, task: GcTask, error: Exception | str) -> None:
        task.state = "pending"
        task.error = str(error)[:4000]
        task.attempts += 1
        delay = min(3600, 30 * (2 ** min(task.attempts - 1, 7)))
        task.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        await self.session.flush()

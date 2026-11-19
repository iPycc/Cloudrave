"""Shared admission control for persistent storage background jobs."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update

from db.base import session_scope
from models.tables import MountSyncTask, PathPlanTask, SYNC_CLAIM_GATE_KEY, SystemMeta

LEASE_DURATION = timedelta(minutes=5)
TASK_MODELS = (MountSyncTask, PathPlanTask)
MAX_ACTIVE_TASKS = 2


def eligible(model, now):
    return or_(
        and_(model.status == "pending", or_(model.lease_expires_at.is_(None), model.lease_expires_at <= now)),
        and_(model.status == "running", or_(model.lease_expires_at.is_(None), model.lease_expires_at < now)),
    )


async def claim_task(session_factory, model):
    # Idle processes must not continuously write to SQLite.
    async with session_scope(session_factory) as session:
        if await session.scalar(select(model.id).where(eligible(model, datetime.now(UTC))).limit(1)) is None:
            return None
    async with session_scope(session_factory) as session:
        # All workers lock this row before inspecting and assigning slots.
        await session.execute(update(SystemMeta).where(SystemMeta.key == SYNC_CLAIM_GATE_KEY).values(value=SystemMeta.value))
        if await session.scalar(select(SystemMeta.key).where(SystemMeta.key == SYNC_CLAIM_GATE_KEY)) is None:
            raise RuntimeError("Background worker coordination row is missing")
        now = datetime.now(UTC)
        owners = []
        for task_model in TASK_MODELS:
            owners.extend(await session.scalars(select(task_model.owner_id).where(
                task_model.status == "running", task_model.lease_expires_at > now
            )))
        if len(owners) >= MAX_ACTIVE_TASKS:
            return None
        query = select(model.id).where(eligible(model, now))
        if owners:
            query = query.where(model.owner_id.not_in(owners))
        task_id = await session.scalar(query.order_by(model.updated_at, model.id).limit(1))
        if task_id is None:
            return None
        token = str(uuid4())
        result = await session.execute(update(model).where(model.id == task_id, eligible(model, now)).values(
            status="running", lease_token=token, lease_expires_at=now + LEASE_DURATION, updated_at=now,
        ))
        return (task_id, token) if result.rowcount == 1 else None

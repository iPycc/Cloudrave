"""Audit log persistence."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tables import AuditLog, User


class AuditLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        actor_id: int | None,
        action: str,
        target: str,
        detail: str | None,
        ip: str | None,
        user_agent: str | None,
        request_id: str | None,
    ) -> AuditLog:
        log = AuditLog(
            actor_id=actor_id,
            action=action,
            target=target,
            detail=detail,
            ip=ip,
            user_agent=user_agent,
            request_id=request_id,
        )
        self.session.add(log)
        await self.session.flush()
        return log

    async def list_login_events(
        self,
        *,
        actor_id: int,
        identifier: str,
        limit: int = 50,
    ) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.action == "login",
                or_(AuditLog.actor_id == actor_id, AuditLog.target == identifier.lower()),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def list_node_events(
        self,
        node_id: int,
        *,
        limit: int = 100,
    ) -> list[tuple[AuditLog, User | None]]:
        stmt = (
            select(AuditLog, User)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .where(AuditLog.target == f"node:{node_id}")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        rows = await self.session.execute(stmt)
        return [(log, user) for log, user in rows.all()]

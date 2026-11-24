"""Admin audit log query and CSV export."""

from __future__ import annotations

import csv
from io import StringIO

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from models.tables import User
from security.rbac import require_admin
from web.mid.auth import get_current_user


router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])


def _csv_cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@router.get("")
async def list_audit_events(
    request: Request,
    action: str | None = Query(default=None, max_length=32),
    actor_id: int | None = Query(default=None, ge=1),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        rows = await dep.get_audit_repository(session).list_events(
            action=action, actor_id=actor_id, before_id=before_id, limit=limit,
        )
        return [
            {
                "id": row.id, "actor_id": row.actor_id, "action": row.action,
                "target": row.target, "detail": row.detail, "ip": row.ip,
                "request_id": row.request_id, "created_at": row.created_at,
            }
            for row in rows
        ]


@router.get("/export")
async def export_audit_events(
    request: Request,
    action: str | None = Query(default=None, max_length=32),
    actor_id: int | None = Query(default=None, ge=1),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=5000, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
) -> Response:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        rows = await dep.get_audit_repository(session).list_events(
            action=action, actor_id=actor_id, before_id=before_id, limit=limit,
        )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(("id", "created_at", "actor_id", "action", "target", "detail", "ip", "request_id"))
    for row in rows:
        writer.writerow((_csv_cell(value) for value in (
            row.id, row.created_at.isoformat(), row.actor_id, row.action,
            row.target, row.detail, row.ip, row.request_id,
        )))
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cloudrave-audit.csv"'},
    )

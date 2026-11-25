"""Admin audit log query and CSV export."""

from __future__ import annotations

import csv
import re
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from models.tables import User
from schemas.common import serialize_utc_datetime
from security.rbac import require_admin
from web.mid.auth import get_current_user


router = APIRouter(prefix="/admin/audit", tags=["admin-audit"])


def _csv_cell(value: object) -> object:
    if isinstance(value, str) and re.match(r"^[\s\x00-\x1f\x7f]*[=+@-]", value):
        return "'" + value
    return value


def _audit_filters(
    request: Request,
    action: str | None = Query(default=None, max_length=32, pattern=r"^[^\x00]*$"),
    actor_id: int | None = Query(default=None, ge=1, le=2**63 - 1),
    before_id: int | None = Query(default=None, ge=1, le=2**63 - 1),
) -> dict:
    maximum = 2**31 - 1 if request.app.state.dep.engine.dialect.name == "postgresql" else 2**63 - 1
    if any(value is not None and value > maximum for value in (actor_id, before_id)):
        raise HTTPException(status_code=422, detail="审计筛选 ID 超出数据库支持范围")
    return {"action": action, "actor_id": actor_id, "before_id": before_id}


@router.get("")
async def list_audit_events(
    request: Request,
    response: Response,
    limit: int = Query(default=100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    filters: dict = Depends(_audit_filters),
) -> list[dict]:
    require_admin(current_user)
    response.headers["Cache-Control"] = "private, no-store"
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        rows = await dep.get_audit_repository(session).list_events(
            **filters, limit=limit,
        )
        return [
            {
                "id": row.id, "actor_id": row.actor_id, "action": row.action,
                "target": row.target, "detail": row.detail, "ip": row.ip,
                "request_id": row.request_id, "created_at": serialize_utc_datetime(row.created_at),
            }
            for row in rows
        ]


@router.get("/export")
async def export_audit_events(
    request: Request,
    limit: int = Query(default=5000, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
    filters: dict = Depends(_audit_filters),
) -> Response:
    require_admin(current_user)
    dep = request.app.state.dep
    async with dep.session_scope() as session:
        rows = await dep.get_audit_repository(session).list_events(
            **filters, limit=limit,
        )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(("id", "created_at", "actor_id", "action", "target", "detail", "ip", "request_id"))
    for row in rows:
        writer.writerow((_csv_cell(value) for value in (
            row.id, serialize_utc_datetime(row.created_at), row.actor_id, row.action,
            row.target, row.detail, row.ip, row.request_id,
        )))
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cloudrave-audit.csv"', "Cache-Control": "private, no-store"},
    )

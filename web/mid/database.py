"""Maintenance gate used while a cross-database copy is active."""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class DatabaseMaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        manager = getattr(request.app.state, "database_manager", None)
        is_write = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
        tracked = False
        if manager is not None and is_write:
            if hasattr(manager, "begin_business_write"):
                tracked = manager.begin_business_write()
                blocked = not tracked
            else:
                blocked = bool(manager.maintenance)
            if blocked:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "database_migration_in_progress",
                        "detail": "数据库迁移进行中，写入操作已暂时暂停",
                    },
                    headers={"Retry-After": "5"},
                )
        try:
            return await call_next(request)
        finally:
            if manager is not None and tracked:
                manager.end_business_write()

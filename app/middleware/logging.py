"""HTTP logging middleware."""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from logging_config import get_logger


LOGGER = get_logger("cloudrave.http")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        LOGGER.info(
            f'{request.method} {request.url.path} -> {response.status_code} {elapsed_ms:.2f}ms',
            extra={
                "request_id": getattr(request.state, "request_id", "-"),
                "ip": request.client.host if request.client else "-",
                "actor": getattr(request.state, "actor", "-"),
            },
        )
        return response

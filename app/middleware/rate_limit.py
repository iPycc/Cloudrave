"""Global and per-endpoint sliding-window rate limiting."""

from __future__ import annotations

from collections import deque
from typing import ClassVar
import time

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Global sliding-window rate limit per client IP."""

    requests_per_window: ClassVar[int] = 120
    window_seconds: ClassVar[float] = 60.0
    _windows: ClassVar[dict[str, deque[float]]] = {}

    async def dispatch(self, request: StarletteRequest, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = self._windows.setdefault(client_ip, deque())
        cutoff = now - self.window_seconds

        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self.requests_per_window:
            request.state.failure_reason = "Rate limit exceeded"
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        window.append(now)
        return await call_next(request)


_LOGIN_LIMIT = 5
_LOGIN_WINDOW_SECONDS = 60.0
_login_windows: dict[str, deque[float]] = {}


def _check_login_window(client_ip: str) -> None:
    now = time.monotonic()
    window = _login_windows.setdefault(client_ip, deque())
    cutoff = now - _LOGIN_WINDOW_SECONDS

    while window and window[0] <= cutoff:
        window.popleft()

    if len(window) >= _LOGIN_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts",
        )

    window.append(now)


async def login_rate_limit(request: Request) -> None:
    """FastAPI dependency for sensitive authentication endpoints."""
    client_ip = request.client.host if request.client else "unknown"
    _check_login_window(client_ip)

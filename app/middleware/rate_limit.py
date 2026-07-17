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
    """Sliding-window rate limits with an isolated budget for previews."""

    requests_per_window: ClassVar[int] = 120
    preview_requests_per_window: ClassVar[int] = 600
    office_source_requests_per_window: ClassVar[int] = 3000
    window_seconds: ClassVar[float] = 60.0
    _windows: ClassVar[dict[tuple[str, str], deque[float]]] = {}
    _last_cleanup: ClassVar[float] = 0.0

    @classmethod
    def _policy_for(cls, request: StarletteRequest) -> tuple[str, int]:
        if request.method in {"GET", "HEAD"} and request.url.path.startswith("/api/v1/explorer/office/source/"):
            return "office-source", cls.office_source_requests_per_window
        if request.method in {"GET", "HEAD"} and (
            request.url.path.startswith("/api/v1/explorer/preview/")
            or request.url.path.startswith("/api/v1/share/") and request.url.path.endswith("/preview")
        ):
            return "preview", cls.preview_requests_per_window
        return "api", cls.requests_per_window

    @classmethod
    def _cleanup_stale_windows(cls, now: float) -> None:
        if now - cls._last_cleanup < cls.window_seconds:
            return

        cutoff = now - cls.window_seconds
        for key, window in list(cls._windows.items()):
            while window and window[0] <= cutoff:
                window.popleft()
            if not window:
                cls._windows.pop(key, None)
        cls._last_cleanup = now

    async def dispatch(self, request: StarletteRequest, call_next):
        client_ip = request.client.host if request.client else "unknown"
        bucket, request_limit = self._policy_for(request)
        now = time.monotonic()
        self._cleanup_stale_windows(now)
        window = self._windows.setdefault((client_ip, bucket), deque())
        cutoff = now - self.window_seconds

        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= request_limit:
            request.state.failure_reason = f"Rate limit exceeded ({bucket})"
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(int(self.window_seconds))},
            )

        window.append(now)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(request_limit)
        response.headers["X-RateLimit-Remaining"] = str(max(request_limit - len(window), 0))
        return response


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

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
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            reason = _single_line(f"{type(exc).__name__}: {exc}")
            LOGGER.exception(
                f'{request.method} {request.url.path} -> 500 {elapsed_ms:.2f}ms reason="{reason}"',
                extra=_log_context(request),
            )
            raise

        elapsed_ms = (time.perf_counter() - started) * 1000
        reason = getattr(request.state, "failure_reason", None)
        message = f'{request.method} {request.url.path} -> {response.status_code} {elapsed_ms:.2f}ms'
        if reason:
            message = f'{message} reason="{_single_line(reason)}"'

        log_method = _response_log_method(response.status_code)
        log_method(message, extra=_log_context(request))
        return response


def _response_log_method(status_code: int):
    if status_code >= 500:
        return LOGGER.error
    if status_code in {401, 403, 429}:
        return LOGGER.warning
    return LOGGER.info


def _single_line(value: object, *, limit: int = 500) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _log_context(request: Request) -> dict[str, str]:
    return {
        "request_id": getattr(request.state, "request_id", "-"),
        "ip": request.client.host if request.client else "-",
        "actor": getattr(request.state, "actor", "-"),
    }

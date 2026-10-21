"""Exception handlers that expose safe failure reasons to request logging."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from drives.remote_drive.common import RemoteAuthenticationError, RemoteProtocolError


_MAX_REASON_LENGTH = 500


def _single_line(value: object) -> str:
    text = " ".join(str(value).split())
    if len(text) <= _MAX_REASON_LENGTH:
        return text
    return f"{text[:_MAX_REASON_LENGTH - 3]}..."


def _http_failure_reason(detail: Any) -> str:
    if isinstance(detail, str):
        return _single_line(detail)
    if isinstance(detail, list):
        messages = [
            _single_line(item.get("msg", item)) if isinstance(item, dict) else _single_line(item)
            for item in detail
        ]
        return _single_line("; ".join(messages))
    return _single_line(detail)


def _validation_failure_reason(exc: RequestValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = _single_line(error.get("msg", "Invalid request"))
        messages.append(f"{location}: {message}" if location else message)
    return _single_line("; ".join(messages) or "Request validation failed")


async def logged_http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> Response:
    request.state.failure_reason = _http_failure_reason(exc.detail)
    return await http_exception_handler(request, exc)


async def logged_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    request.state.failure_reason = _validation_failure_reason(exc)
    return await request_validation_exception_handler(request, exc)


async def remote_protocol_exception_handler(
    request: Request,
    exc: RemoteProtocolError,
) -> Response:
    detail = _single_line(exc)
    request.state.failure_reason = detail
    if isinstance(exc, RemoteAuthenticationError):
        return JSONResponse(status_code=409, content={"detail": detail, "code": "storage_reauthorization_required"})
    return JSONResponse(status_code=502, content={"detail": detail})


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, logged_http_exception_handler)
    app.add_exception_handler(RequestValidationError, logged_validation_exception_handler)
    app.add_exception_handler(RemoteProtocolError, remote_protocol_exception_handler)

"""Exception handlers that expose safe failure reasons to request logging."""

from __future__ import annotations

import json
from typing import Any

from alibabacloud_oss_v2.exceptions import BaseError as OssSdkError
from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from qcloud_cos.cos_exception import CosException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from drives.remote_drive.common import RemoteAuthenticationError, RemoteProtocolError
from store.errors import storage_error_details


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
    response = await http_exception_handler(request, exc)
    payload = json.loads(response.body)
    payload.setdefault("code", {
        401: "authentication_required", 403: "permission_denied", 404: "not_found",
        409: "conflict", 413: "quota_exceeded", 429: "rate_limited",
    }.get(exc.status_code, "request_failed" if exc.status_code < 500 else "service_unavailable"))
    if isinstance(exc.detail, dict):
        payload["message"] = str(exc.detail.get("message") or exc.detail.get("detail") or "请求失败")
        payload["code"] = str(exc.detail.get("code") or payload["code"])
    elif isinstance(exc.__cause__, (CosException, OssSdkError)) and exc.detail == str(exc.__cause__):
        payload.update(storage_error_details(exc.__cause__))
        payload["detail"] = payload["message"]
    else:
        payload["message"] = _http_failure_reason(exc.detail)
    payload["request_id"] = request.state.request_id
    return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)


async def logged_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    request.state.failure_reason = _validation_failure_reason(exc)
    response = await request_validation_exception_handler(request, exc)
    payload = json.loads(response.body)
    payload.update(message=_validation_failure_reason(exc), code="validation_error", request_id=request.state.request_id)
    return JSONResponse(status_code=response.status_code, content=payload)


async def remote_protocol_exception_handler(
    request: Request,
    exc: RemoteProtocolError,
) -> Response:
    detail = _single_line(exc)
    request.state.failure_reason = detail
    if isinstance(exc, RemoteAuthenticationError):
        return JSONResponse(status_code=409, content={"detail": detail, "message": detail, "code": "storage_reauthorization_required",
                                                     "request_id": request.state.request_id})
    return JSONResponse(status_code=502, content={"detail": detail, "message": detail, "code": exc.code or "storage_unavailable",
                                                 "request_id": request.state.request_id})


async def unexpected_exception_handler(request: Request, exc: Exception) -> Response:
    request.state.failure_reason = _single_line(exc)
    return JSONResponse(status_code=500, content={"detail": "服务器暂时无法完成请求", "message": "服务器暂时无法完成请求", "code": "internal_error",
                                                  "request_id": request.state.request_id})


class UnexpectedErrorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            return await unexpected_exception_handler(request, exc)


async def storage_exception_handler(request: Request, exc: Exception) -> Response:
    error = storage_error_details(exc)
    request.state.failure_reason = error["message"]
    return JSONResponse(status_code=502, content={**error, "detail": error["message"],
                                                 "request_id": request.state.request_id})


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, logged_http_exception_handler)
    app.add_exception_handler(RequestValidationError, logged_validation_exception_handler)
    app.add_exception_handler(RemoteProtocolError, remote_protocol_exception_handler)
    app.add_exception_handler(CosException, storage_exception_handler)
    app.add_exception_handler(OssSdkError, storage_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)

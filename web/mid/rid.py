"""Assign request ids to incoming HTTP requests."""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class RequestIdMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self.header_name = header_name.strip() or "X-Request-ID"

    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get(self.header_name, "")
        request_id = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", supplied) else str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[self.header_name] = request_id
        return response

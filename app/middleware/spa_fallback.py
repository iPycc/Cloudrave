"""Raw ASGI middleware that serves index.html for non-API 404 responses."""

from __future__ import annotations

import asyncio
from pathlib import Path


class SpaFallbackMiddleware:
    """Return the SPA entrypoint when the app responds with 404 on a page load."""

    def __init__(self, app, index_html: Path) -> None:
        self.app = app
        self.index_html = index_html

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        accept = headers.get(b"accept", b"").decode("latin-1")
        if "text/html" not in accept and "*/*" not in accept:
            await self.app(scope, receive, send)
            return

        status_code = None
        response_started = False

        async def wrapped_send(message):
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if status_code != 404:
                    response_started = True
                    await send(message)
                    return
                # Replace 404 with index.html
                response_started = True
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/html; charset=utf-8"),
                    ],
                })
            elif message["type"] == "http.response.body" and status_code == 404:
                if not self.index_html.is_file():
                    await send({
                        "type": "http.response.body",
                        "body": b"Not Found",
                        "more_body": False,
                    })
                    return
                if method == "HEAD":
                    await send({
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": False,
                    })
                else:
                    body = await asyncio.to_thread(self.index_html.read_bytes)
                    await send({
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False,
                    })
            else:
                await send(message)

        await self.app(scope, receive, wrapped_send)

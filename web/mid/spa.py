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
        if path in ("/api", "/assets") or path.startswith(("/api/", "/assets/")):
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

        fallback_body = None

        async def wrapped_send(message):
            nonlocal fallback_body
            if message["type"] == "http.response.start":
                if message["status"] == 404:
                    try:
                        fallback_body = await asyncio.to_thread(self.index_html.read_bytes)
                    except OSError:
                        # Preserve the original 404 if the entrypoint is unavailable.
                        pass
                    else:
                        # Retain security, request-id and cookie headers, replacing
                        # metadata belonging to the original error document.
                        replaced_headers = {
                            b"content-type", b"content-length", b"content-encoding",
                            b"content-range", b"etag", b"last-modified", b"cache-control",
                        }
                        response_headers = [
                            (key, value) for key, value in message.get("headers", [])
                            if key.lower() not in replaced_headers
                        ]
                        response_headers.extend([
                            (b"content-type", b"text/html; charset=utf-8"),
                            (b"content-length", str(len(fallback_body)).encode("ascii")),
                            (b"cache-control", b"no-cache"),
                        ])
                        message = {**message, "status": 200, "headers": response_headers}
                await send(message)
            elif message["type"] == "http.response.body" and fallback_body is not None:
                # BaseHTTPMiddleware streams the 404 and then emits an empty
                # final chunk. Drain all chunks and finish the replacement once.
                if not message.get("more_body", False):
                    await send({
                        "type": "http.response.body",
                        "body": b"" if method == "HEAD" else fallback_body,
                        "more_body": False,
                    })
            else:
                await send(message)

        await self.app(scope, receive, wrapped_send)

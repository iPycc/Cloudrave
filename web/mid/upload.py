"""Bound avatar request bodies before multipart parsing can fill temporary disk."""
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


class AvatarBodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = 6 * 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root + "/"):
            path = path[len(root):]
        if scope["type"] != "http" or scope.get("method") != "POST" or path != "/api/v1/user/me/avatar":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        rejection = JSONResponse({"detail": "Avatar request is too large", "message": "Avatar request is too large",
                                  "code": "request_too_large"}, status_code=413)
        if declared > self.max_bytes:
            return await rejection(scope, receive, send)
        received = 0
        exceeded = False

        async def bounded_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    # Starlette closes any multipart temporary files on this exception.
                    raise MultiPartException("Avatar request is too large")
            return message

        async def bounded_send(message):
            if not exceeded:
                await send(message)

        try:
            await self.app(scope, bounded_receive, bounded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await rejection(scope, receive, send)

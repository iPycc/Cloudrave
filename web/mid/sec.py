"""Security headers middleware."""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


_API_DOCS_PATHS = {
    "/api/docs",
    "/api/redoc",
    "/docs/oauth2-redirect",
}

_DEFAULT_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "connect-src 'self' https:; "
    "media-src 'self' blob: https:; "
    "frame-src 'self' https://view.officeapps.live.com https://*.officeapps.live.com; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# FastAPI's development-only Swagger UI and ReDoc pages load their official
# assets from jsDelivr. Swagger UI also uses an inline bootstrap script, while
# ReDoc loads its default fonts from Google Fonts. Keep these exceptions scoped
# to FastAPI's documentation routes instead of weakening the application CSP.
_API_DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "connect-src 'self' https:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            _API_DOCS_CONTENT_SECURITY_POLICY
            if request.url.path in _API_DOCS_PATHS
            else _DEFAULT_CONTENT_SECURITY_POLICY
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

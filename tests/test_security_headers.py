from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from web.mid.sec import SecurityHeadersMiddleware


def test_security_headers_preserve_preview_sandbox_and_default_other_routes():
    async def preview(request):
        return Response("<svg/>", media_type="image/svg+xml", headers={
            "Content-Security-Policy": "sandbox; default-src 'none'"})

    async def normal(request):
        return Response("ok")

    app = Starlette(routes=[Route("/preview", preview), Route("/normal", normal)])
    app.add_middleware(SecurityHeadersMiddleware)
    with TestClient(app) as client:
        response = client.get("/preview")
        assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self'" in client.get("/normal").headers["content-security-policy"]

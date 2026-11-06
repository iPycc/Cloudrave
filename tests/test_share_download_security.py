from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.responses import Response

from api.v1 import share as routes
from api.v1 import shared as mounted_routes
from models.enums import NodeType
from services.explorer.shared_io import SharedIo
from web.mid.auth import get_current_user


@asynccontextmanager
async def scope():
    yield object()


def test_limited_share_blocks_all_preview_paths_and_does_not_issue_head_url(monkeypatch):
    share = SimpleNamespace(id="audit", access=SimpleNamespace(value="public"), max_downloads=1, download_count=0)
    node = SimpleNamespace(id=1, name="file.txt", type=NodeType.FILE)
    counted = []

    class Service:
        @property
        def source(self):
            return self

        async def record_download(self, current):
            counted.append(current.id)

        async def get_share_info(self, share_id):
            return share, [node]

        async def resolve_shared_node(self, share, node_id):
            return node

        async def resolve_node(self, actor, mount_id, node_id):
            return share, node

    class Source:
        def __init__(self, **kwargs):
            pass

        async def resolve_shared(self, node_id):
            return node, object(), object()

        async def probe_response(self, *args):
            pytest.fail("A limited share must not issue an uncounted signed URL")

        async def response(self, *args, **kwargs):
            assert kwargs["relay"] is True
            return Response(b"downloaded")

    monkeypatch.setattr(routes, "_build_share_service", lambda *args: Service())
    monkeypatch.setattr(routes, "DownloadFileService", Source)
    monkeypatch.setattr(mounted_routes, "_service", lambda *args: Service())
    monkeypatch.setattr(mounted_routes, "_io", lambda *args: SharedIo(shared=Service(), source=Source(), archive=None))
    dep = SimpleNamespace(session_scope=scope, **{f"get_{name}_repository": lambda session: None
        for name in ("mount", "node", "policy", "object")}, storage_manager=None)
    app = FastAPI()
    app.state.dep = dep
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    app.include_router(routes.router)
    app.include_router(mounted_routes.router)
    with TestClient(app) as client:
        for path in ("/share/audit/preview", "/share/audit/preview/manifest",
                     "/share/audit/preview/archive/entry?path=secret.txt", "/share/audit/cover",
                     "/shared/1/preview?node_id=1", "/shared/1/preview/manifest?node_id=1",
                     "/shared/1/preview/archive/entry?node_id=1&path=secret.txt"):
            assert client.get(path).status_code == 403, path
        for path in ("/share/audit/download", "/shared/1/download?node_id=1"):
            response = client.head(path)
            assert response.status_code == 204
            assert "x-cloudrave-download-url" not in response.headers
            assert response.headers["x-cloudrave-download-mode"] == "cloudrave"
        assert counted == []
        for path in ("/share/audit/download", "/shared/1/download?node_id=1"):
            assert client.get(path).content == b"downloaded"
        assert counted == ["audit", "audit"]

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.middleware.rate_limit import RateLimitMiddleware
from app.models.enums import NodeStatus, NodeType
from app.models.tables import Node
from app.repositories.nodes import NodeRepository
from app.services.explorer.file import _decode_page_cursor, _encode_page_cursor
from app.services.explorer.preview import build_local_preview_response
from database import Base


def test_page_cursor_round_trip() -> None:
    cursor = _encode_page_cursor(400)
    assert _decode_page_cursor(cursor) == 400
    assert _decode_page_cursor(None) == 0


def test_invalid_page_cursor_is_rejected() -> None:
    with pytest.raises(HTTPException) as error:
        _decode_page_cursor("not-a-cursor")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_node_pages_are_folder_first_and_filterable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        root = Node(
            owner_id=1,
            mount_id=1,
            parent_id=None,
            type=NodeType.FOLDER,
            status=NodeStatus.ACTIVE,
            name="root",
        )
        session.add(root)
        await session.flush()
        session.add_all(
            [
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FOLDER, status=NodeStatus.ACTIVE, name="beta"),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FOLDER, status=NodeStatus.ACTIVE, name="alpha"),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="z.txt", size=3),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="a.txt", size=1),
                Node(owner_id=1, mount_id=1, parent_id=root.id, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="photo.JPG", size=2),
            ]
        )
        await session.flush()

        repository = NodeRepository(session)
        first_page, first_has_more = await repository.list_children_page(
            1,
            root.id,
            limit=2,
            offset=0,
            sort="name-asc",
        )
        assert [node.name for node in first_page] == ["alpha", "beta"]
        assert first_has_more is True

        folder_page, folder_has_more = await repository.list_children_page(
            1,
            root.id,
            limit=10,
            offset=0,
            sort="name-asc",
            folders_only=True,
        )
        assert [node.name for node in folder_page] == ["alpha", "beta"]
        assert folder_has_more is False

        image_page, image_has_more = await repository.list_category_page(
            1,
            "image",
            limit=10,
            offset=0,
            sort="name-asc",
        )
        assert [node.name for node in image_page] == ["photo.JPG"]
        assert image_has_more is False

        search_results = await repository.search(1, "PHOTO", limit=20)
        assert [node.name for node in search_results] == ["photo.JPG"]

    await engine.dispose()


def test_local_preview_supports_ranges_and_conditional_cache(tmp_path) -> None:
    preview_file = tmp_path / "sample.bin"
    preview_file.write_bytes(b"0123456789")

    app = FastAPI()

    @app.get("/preview")
    async def preview(request: Request):
        return build_local_preview_response(
            request,
            preview_file,
            filename="sample.bin",
            media_type="application/octet-stream",
        )

    with TestClient(app) as client:
        full_response = client.get("/preview")
        range_response = client.get("/preview", headers={"Range": "bytes=2-5"})
        cached_response = client.get(
            "/preview",
            headers={"If-None-Match": full_response.headers["etag"]},
        )

    assert full_response.status_code == 200
    assert full_response.content == b"0123456789"
    assert full_response.headers["accept-ranges"] == "bytes"
    assert full_response.headers["cache-control"] == "private, max-age=3600"

    assert range_response.status_code == 206
    assert range_response.content == b"2345"
    assert range_response.headers["content-range"] == "bytes 2-5/10"

    assert cached_response.status_code == 304
    assert cached_response.content == b""
    assert cached_response.headers["etag"] == full_response.headers["etag"]


def test_preview_requests_use_an_isolated_rate_limit_budget() -> None:
    preview_scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/explorer/preview/42",
        "query_string": b"",
        "headers": [],
        "scheme": "https",
        "server": ("testserver", 443),
        "client": ("127.0.0.1", 1234),
    }
    api_scope = {**preview_scope, "path": "/api/v1/explorer/node/page"}

    preview_bucket, preview_limit = RateLimitMiddleware._policy_for(Request(preview_scope))
    api_bucket, api_limit = RateLimitMiddleware._policy_for(Request(api_scope))

    assert (preview_bucket, preview_limit) == ("preview", 600)
    assert (api_bucket, api_limit) == ("api", 120)

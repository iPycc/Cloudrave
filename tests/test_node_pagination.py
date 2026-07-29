from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.enums import NodeStatus, NodeType, ShareAccess
from models.tables import Node
from repositories.nodes import NodeRepository
from repositories.shares import ShareRepository
from security.jwt import AuthError, TokenService
from services.explorer.file import _decode_page_cursor, _encode_page_cursor
from services.explorer.share import ShareService
from web.mid.rate import RateLimitMiddleware
from PIL import Image
from starlette.datastructures import URL

from services.explorer.preview import (
    PreviewService,
    build_local_preview_response,
    preview_kind,
    preview_version,
    relative_preview_url,
)
from core.cfg import AuthConfig
from db.base import Base


def test_page_cursor_round_trip() -> None:
    cursor = _encode_page_cursor(400)
    assert _decode_page_cursor(cursor) == 400
    assert _decode_page_cursor(None) == 0


def test_invalid_page_cursor_is_rejected() -> None:
    with pytest.raises(HTTPException) as error:
        _decode_page_cursor("not-a-cursor")
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_one_share_can_hold_multiple_roots_and_cannot_escape_them() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        folder = Node(owner_id=1, mount_id=1, parent_id=None, type=NodeType.FOLDER, status=NodeStatus.ACTIVE, name="folder")
        second_root = Node(owner_id=1, mount_id=1, parent_id=None, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="second.txt", size=2)
        outside = Node(owner_id=1, mount_id=1, parent_id=None, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="outside.txt", size=3)
        session.add_all([folder, second_root, outside])
        await session.flush()
        child = Node(owner_id=1, mount_id=1, parent_id=folder.id, type=NodeType.FILE, status=NodeStatus.ACTIVE, name="child.txt", size=1)
        session.add(child)
        await session.flush()

        shares = ShareRepository(session)
        share = await shares.create(
            owner_id=1,
            node_ids=[folder.id, second_root.id],
            access=ShareAccess.PUBLIC,
            password_hash=None,
            expires_at=None,
            max_downloads=None,
        )
        assert await shares.list_root_node_ids(share) == [folder.id, second_root.id]

        service = ShareService(
            session=session,
            shares=shares,
            nodes=NodeRepository(session),
            audits=None,  # type: ignore[arg-type]
            share_signing_key=b"test-key",
        )
        assert (await service.resolve_shared_node(share, child.id)).id == child.id
        assert (await service.resolve_shared_node(share, second_root.id)).id == second_root.id
        with pytest.raises(HTTPException) as error:
            await service.resolve_shared_node(share, outside.id)
        assert error.value.status_code == 404

    await engine.dispose()


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
    office_scope = {**preview_scope, "path": "/api/v1/explorer/office/source/42"}
    shared_preview_scope = {**preview_scope, "path": "/api/v1/share/abc123/preview"}

    preview_bucket, preview_limit = RateLimitMiddleware._policy_for(Request(preview_scope))
    api_bucket, api_limit = RateLimitMiddleware._policy_for(Request(api_scope))
    office_bucket, office_limit = RateLimitMiddleware._policy_for(Request(office_scope))
    shared_preview_bucket, shared_preview_limit = RateLimitMiddleware._policy_for(Request(shared_preview_scope))

    assert (preview_bucket, preview_limit) == ("preview", 600)
    assert (api_bucket, api_limit) == ("api", 120)
    assert (office_bucket, office_limit) == ("office-source", 3000)
    assert (shared_preview_bucket, shared_preview_limit) == ("preview", 600)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("photo.JPEG", "image"),
        ("movie.mkv", "video"),
        ("track.flac", "audio"),
        ("manual.pdf", "pdf"),
        ("budget.xlsx", "office"),
        ("main.tsx", "text"),
        ("backup.tar", "archive"),
        ("disk.iso", "unsupported"),
    ],
)
def test_preview_kind_routes_supported_formats(name: str, kind: str) -> None:
    assert preview_kind(name) == kind


def test_preview_version_changes_with_file_metadata() -> None:
    from datetime import UTC, datetime

    node = Node(
        id=99,
        owner_id=1,
        mount_id=1,
        type=NodeType.FILE,
        status=NodeStatus.ACTIVE,
        name="photo.jpg",
        size=10,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    first = preview_version(node)
    node.size = 11
    assert preview_version(node) != first


def test_scoped_preview_tokens_cannot_be_reused_for_another_purpose() -> None:
    service = TokenService(AuthConfig(jwt_secret="x" * 40, jwt_issuer="preview-tests"))
    token = service.issue_scoped_token(
        sub="42",
        purpose="office-source",
        ttl_seconds=60,
        claims={"node_id": 7, "version": "abc"},
    )

    payload = service.parse_scoped_token(token, "office-source")
    assert payload["node_id"] == 7
    assert payload["version"] == "abc"
    with pytest.raises(AuthError):
        service.parse_scoped_token(token, "two-factor")


def test_image_derivative_is_resized_and_written_atomically(tmp_path, monkeypatch) -> None:
    source = BytesIO()
    Image.new("RGB", (1600, 900), "#336699").save(source, format="PNG")
    payload = source.getvalue()
    service = object.__new__(PreviewService)
    monkeypatch.setattr(service, "_open_seekable", lambda *_args: BytesIO(payload))
    target = tmp_path / "thumbnail.webp"

    service._generate_image_variant(object(), object(), object(), target, 320)

    assert target.is_file()
    with Image.open(target) as derivative:
        assert derivative.format == "WEBP"
        assert derivative.size == (320, 180)


def test_browser_media_compatibility_is_conservative() -> None:
    service = object.__new__(PreviewService)
    assert service._browser_compatible("video", "clip.mp4", {"video_codec": "h264", "audio_codec": "aac"}) is True
    assert service._browser_compatible("video", "clip.mp4", {"video_codec": "hevc", "audio_codec": "aac"}) is False
    assert service._browser_compatible("video", "clip.mkv", {"video_codec": "h264", "audio_codec": "aac"}) is False


def test_audio_metadata_and_embedded_cover_are_extracted(tmp_path, monkeypatch) -> None:
    cover_buffer = BytesIO()
    Image.new("RGB", (1400, 900), "#224466").save(cover_buffer, format="JPEG")

    easy_audio = SimpleNamespace(
        tags={
            "title": ["Marry"],
            "artist": ["Top Barry"],
            "album": ["Demo Album"],
            "albumartist": ["INDEcompany"],
            "tracknumber": ["3/10"],
            "genre": ["Pop"],
        },
        info=SimpleNamespace(length=183.25, bitrate=320000, sample_rate=44100, channels=2),
    )

    class RawTags:
        @staticmethod
        def getall(key: str):
            return [SimpleNamespace(data=cover_buffer.getvalue())] if key == "APIC" else []

        @staticmethod
        def get(_key: str):
            return None

    raw_audio = SimpleNamespace(tags=RawTags())
    audio_results = iter((easy_audio, raw_audio))
    monkeypatch.setattr("services.explorer.preview.MutagenFile", lambda *_args, **_kwargs: next(audio_results))

    service = object.__new__(PreviewService)
    monkeypatch.setattr(service, "_probe_media", lambda *_args: {"audio_codec": "mp3"})
    monkeypatch.setattr(service, "_open_seekable", lambda *_args: BytesIO(b"fake audio"))
    target = tmp_path / "cover.webp"

    metadata = service._probe_audio(object(), object(), object(), target)

    assert metadata["title"] == "Marry"
    assert metadata["artist"] == "Top Barry"
    assert metadata["album"] == "Demo Album"
    assert metadata["album_artist"] == "INDEcompany"
    assert metadata["track"] == "3/10"
    assert metadata["duration"] == 183.25
    assert metadata["bitrate"] == 320000
    assert metadata["cover_available"] is True
    with Image.open(target) as cover:
        assert cover.format == "WEBP"
        assert cover.size == (1024, 658)


def test_internal_preview_urls_are_same_origin_paths() -> None:
    assert relative_preview_url(URL("http://127.0.0.1:1309/api/v1/explorer/preview/14")) == "/api/v1/explorer/preview/14"
    assert (
        relative_preview_url(URL("http://127.0.0.1:1309/api/v1/explorer/preview/14/image/screen?v=abc"))
        == "/api/v1/explorer/preview/14/image/screen?v=abc"
    )

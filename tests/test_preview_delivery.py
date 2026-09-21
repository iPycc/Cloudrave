from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
import py7zr
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from starlette.datastructures import URL
from starlette.requests import Request

from models.enums import StoragePolicyType
from schemas.preview import PreviewAssetRead, PreviewManifestRead
from schemas.preview import PdfAnnotationItem
from services.explorer.extract import _is_selected
from services.explorer.preview import PreviewService
from services.explorer.proxy import remote_response
from services.explorer.view.arc import ArcMixin
from services.explorer.view.img import _safe_exif
from store.types import PresignedURL


class _RouteDep:
    def __init__(self) -> None:
        self.storage_manager = object()
        self.token_service = object()
        self.share_signing_key = b"test-signing-key"
        self.annotation_repository = object()
        self.config = SimpleNamespace(
            preview_cache_root_path=Path("."),
            app=SimpleNamespace(
                public_base_url="https://cloudrave.test",
                office_source_ttl_seconds=900,
                text_preview_max_bytes=1024,
            ),
        )

    def __getattr__(self, name: str):
        if not (name.startswith("get_") and name.endswith("_repository")):
            raise AttributeError(name)
        repository = self.annotation_repository if name == "get_preview_annotation_repository" else object()
        return lambda _session: repository


def test_route_factories_inject_annotations_only_into_preview_services() -> None:
    from api.v1.explore.dep import node_service, preview_service
    from api.v1.share import _build_preview_service, _build_share_service
    from api.v1.shared import _preview, _service

    dep = _RouteDep()
    session = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(dep=dep)))

    assert node_service(dep, session).storage_manager is dep.storage_manager
    assert preview_service(dep, session).annotations is dep.annotation_repository
    assert _build_share_service(request, session).share_signing_key is dep.share_signing_key
    assert _build_preview_service(request, session).annotations is dep.annotation_repository
    assert _service(request, session).source.share_signing_key is dep.share_signing_key
    assert _preview(request, session).annotations is dep.annotation_repository


class _Request:
    def url_for(self, name: str, **params: object) -> URL:
        assert name == "preview_file"
        return URL(f"https://cloudrave.test/api/v1/explorer/preview/{params['node_id']}")


class _SignedAdapter:
    def __init__(self, expires_at: datetime) -> None:
        self.expires_at = expires_at
        self.head_calls: list[str] = []
        self.calls: list[dict[str, object]] = []

    async def ahead_object(self, _policy, _mount, object_key: str):
        self.head_calls.append(object_key)
        return SimpleNamespace(size=1024)

    async def abuild_download_url(self, _policy, _mount, object_key: str, **kwargs):
        self.calls.append({"object_key": object_key, **kwargs})
        return PresignedURL(
            method="GET",
            url="https://bucket.cos.example/report.pdf?signature=temporary",
            expires_at=self.expires_at,
        )


class _StorageManager:
    def __init__(self, adapter: _SignedAdapter) -> None:
        self.adapter = adapter

    def get_adapter(self, _policy):
        return self.adapter

    def local_path(self, policy, _mount, object_key: str):
        provider = getattr(policy.provider, "value", policy.provider)
        return Path(object_key) if provider == "local_fs" else None


def _service_with_adapter(adapter: _SignedAdapter) -> PreviewService:
    service = object.__new__(PreviewService)
    service.storage_manager = _StorageManager(adapter)
    return service


def test_cos_preview_source_is_signed_direct_with_proxy_fallback() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    adapter = _SignedAdapter(expires_at)
    service = _service_with_adapter(adapter)
    node = SimpleNamespace(id=42, name="report.pdf", size=1024, blob_path="managed/report.pdf")
    policy = SimpleNamespace(provider=StoragePolicyType.TENCENT_COS)

    assets = asyncio.run(
        service._source_assets(
            _Request(),
            node,
            SimpleNamespace(),
            policy,
            "version-1",
            "application/pdf",
        )
    )

    assert assets["source"].delivery == "storage"
    assert assets["source"].url.startswith("https://bucket.cos.example/")
    assert assets["source"].expires_at == expires_at
    assert assets["proxy"].delivery == "cloudrave"
    assert assets["proxy"].url == "/api/v1/explorer/preview/42?v=version-1"
    assert adapter.head_calls == ["managed/report.pdf"]
    assert adapter.calls == [
        {
            "object_key": "managed/report.pdf",
            "download_name": "report.pdf",
            "inline": True,
        }
    ]


def test_local_preview_source_stays_on_cloudrave() -> None:
    adapter = _SignedAdapter(datetime.now(UTC))
    service = _service_with_adapter(adapter)
    node = SimpleNamespace(id=7, name="photo.jpg", size=2048, blob_path="managed/photo.jpg")
    policy = SimpleNamespace(provider=StoragePolicyType.LOCAL_FS)

    assets = asyncio.run(
        service._source_assets(
            _Request(),
            node,
            SimpleNamespace(),
            policy,
            "version-2",
            "image/jpeg",
        )
    )

    assert list(assets) == ["source"]
    assert assets["source"].delivery == "cloudrave"
    assert assets["source"].url == "/api/v1/explorer/preview/7?v=version-2"
    assert adapter.head_calls == []
    assert adapter.calls == []


def test_shared_manifest_keeps_signed_source_and_uses_share_route_as_fallback() -> None:
    service = object.__new__(PreviewService)
    resolved = (SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    class _Resolver:
        async def resolve_shared(self, _node_id: int):
            return resolved

    async def _build_manifest(self, _request, _node, _mount, _policy):
        return PreviewManifestRead(
            node_id=9,
            name="shared.pdf",
            version="shared-version",
            kind="pdf",
            mime_type="application/pdf",
            size=4096,
            assets={
                "source": PreviewAssetRead(
                    url="https://bucket.cos.example/shared.pdf?signature=temporary",
                    mime_type="application/pdf",
                    size=4096,
                    supports_range=True,
                    delivery="storage",
                )
            },
        )

    service.resolver = _Resolver()
    service._build_manifest = MethodType(_build_manifest, service)

    manifest = asyncio.run(
        service.shared_manifest(
            _Request(),
            9,
            source_url="/api/v1/share/public/preview?node_id=9",
        )
    )

    assert manifest.assets["source"].delivery == "storage"
    assert manifest.assets["source"].url.startswith("https://bucket.cos.example/")
    assert manifest.assets["proxy"].delivery == "cloudrave"
    assert manifest.assets["proxy"].url == "/api/v1/share/public/preview?node_id=9"


def test_remote_preview_maps_missing_cos_object_to_404() -> None:
    class _MissingResource(Exception):
        def get_error_code(self) -> str:
            return "NoSuchResource"

    class _MissingAdapter:
        async def ahead_object(self, *_args, **_kwargs):
            raise _MissingResource()

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/explorer/preview/343",
            "raw_path": b"/api/v1/explorer/preview/343",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("cloudrave.test", 443),
        }
    )

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            remote_response(
                request,
                adapter=_MissingAdapter(),
                policy=SimpleNamespace(),
                mount=SimpleNamespace(),
                object_key="blob/missing.md",
                filename="missing.md",
            )
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == "预览源文件在存储中不存在"


def test_preview_manifest_maps_missing_cos_object_to_404() -> None:
    class _MissingResource(Exception):
        def get_error_code(self) -> str:
            return "NoSuchResource"

    class _MissingAdapter:
        async def ahead_object(self, *_args, **_kwargs):
            raise _MissingResource()

    service = object.__new__(PreviewService)
    service.storage_manager = SimpleNamespace(
        get_adapter=lambda _policy: _MissingAdapter(),
        local_path=lambda _policy, _mount, _object_key: None,
    )
    node = SimpleNamespace(
        id=343,
        name="CHANGELOG.md",
        size=2320,
        blob_path="blob/missing-CHANGELOG.md",
    )

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            service._source_assets(
                _Request(),
                node,
                SimpleNamespace(),
                SimpleNamespace(provider=StoragePolicyType.TENCENT_COS),
                "version-missing",
                "text/markdown",
            )
        )

    assert captured.value.status_code == 404
    assert captured.value.detail == "预览源文件在存储中不存在"


def test_7z_entries_and_entry_preview() -> None:
    stream = BytesIO()
    with py7zr.SevenZipFile(stream, mode="w") as archive:
        archive.writestr(b"Cloudrave preview", "docs/readme.txt")
    payload = stream.getvalue()

    service = object.__new__(ArcMixin)
    service._open_seekable = lambda *_args: BytesIO(payload)
    node = SimpleNamespace(name="sample.7z")

    entries = service._archive_entries(node, SimpleNamespace(), SimpleNamespace())
    assert entries[0]["name"] == "docs/readme.txt"
    assert entries[0]["size"] == 17
    assert entries[0]["directory"] is False
    assert service._read_archive_entry(
        node,
        SimpleNamespace(),
        SimpleNamespace(),
        "docs/readme.txt",
    ) == b"Cloudrave preview"


def test_private_preview_exif_omits_gps() -> None:
    image = Image.new("RGB", (10, 10))
    exif = image.getexif()
    exif[271] = "Cloudrave Camera"
    exif[34853] = {1: "N", 2: (31.0, 12.0, 0.0)}

    metadata = _safe_exif(image)

    assert metadata == {"camera_make": "Cloudrave Camera"}


def test_pdf_annotation_coordinates_are_normalized() -> None:
    with pytest.raises(ValidationError):
        PdfAnnotationItem(
            id="outside-page",
            page=1,
            kind="pen",
            x=0.5,
            y=0.5,
            points=[[1.1, 0.5]],
        )


def test_archive_selection_includes_descendants() -> None:
    selected = ["docs"]
    assert _is_selected("docs/readme.txt", selected)
    assert _is_selected("docs", selected)
    assert not _is_selected("images/cover.png", selected)

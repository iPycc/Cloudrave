from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace

from starlette.datastructures import URL

from app.models.enums import StoragePolicyType
from app.schemas.preview import PreviewAssetRead, PreviewManifestRead
from app.services.explorer.preview import PreviewService
from app.storage.types import PresignedURL


class _Request:
    def url_for(self, name: str, **params: object) -> URL:
        assert name == "preview_file"
        return URL(f"https://cloudrave.test/api/v1/explorer/preview/{params['node_id']}")


class _SignedAdapter:
    def __init__(self, expires_at: datetime) -> None:
        self.expires_at = expires_at
        self.calls: list[dict[str, object]] = []

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

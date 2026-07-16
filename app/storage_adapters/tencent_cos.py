"""Tencent COS storage adapter skeleton."""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode, quote
from uuid import uuid4

from app.storage_adapters.base import StorageAdapterBase
from app.storage.types import (
    MultipartPart,
    MultipartUploadState,
    PresignedURL,
    StorageCapability,
    StorageObject,
    normalize_object_key,
    utcnow,
)


@dataclass(slots=True)
class TencentCosCredentials:
    secret_id: str = ""
    secret_key: str = ""
    token: str | None = None
    bucket: str = ""
    region: str = ""
    endpoint: str | None = None


class TencentCosAdapter(StorageAdapterBase):
    provider_type = "tencent_cos"
    capabilities = StorageCapability(
        supports_presign=True,
        supports_multipart=True,
        supports_cors_autoconfig=True,
        supports_direct_write=False,
    )

    def __init__(self) -> None:
        self._stub_prefix = "https://cos.cloudrave.local"

    def prepare_mount_root(self, mount) -> Path:  # type: ignore[override]
        return Path.cwd() / "upload" / "cos" / mount.mount_slug

    def _creds(self, policy) -> TencentCosCredentials:
        provider_config = getattr(policy, "extra", {}) or {}
        provider_config = provider_config.get("provider_config", provider_config)
        return TencentCosCredentials(
            secret_id=str(getattr(policy, "secret_id", None) or provider_config.get("secret_id", "")),
            secret_key=str(getattr(policy, "secret_key", None) or provider_config.get("secret_key", "")),
            token=getattr(policy, "session_token", None) or provider_config.get("token"),
            bucket=str(getattr(policy, "bucket_name", None) or provider_config.get("bucket", "")),
            region=str(getattr(policy, "region", None) or provider_config.get("region", "")),
            endpoint=getattr(policy, "endpoint", None) or provider_config.get("endpoint"),
        )

    def _build_stub_signature(self, method: str, bucket: str, key: str, upload_id: str | None = None, part_number: int | None = None) -> str:
        payload = f"{method}:{bucket}:{key}:{upload_id or ''}:{part_number or ''}".encode("utf-8")
        return base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii").rstrip("=")

    def _build_object_url(self, policy, mount, object_key: str) -> str:
        creds = self._creds(policy)
        normalized_key = quote(normalize_object_key(object_key))
        bucket = creds.bucket or mount.mount_slug
        region = creds.region or "ap-guangzhou"
        endpoint = creds.endpoint or f"{bucket}.cos.{region}.myqcloud.com"
        return f"https://{endpoint}/{normalized_key}"

    def put_object(self, policy, mount, object_key: str, body, *, mime_type: str | None = None, metadata: dict[str, str] | None = None) -> StorageObject:  # type: ignore[override]
        raise NotImplementedError("Tencent COS direct write will be wired through presigned upload URLs.")

    def open_object(self, policy, mount, object_key: str):  # type: ignore[override]
        raise NotImplementedError("Tencent COS download stream will be handled by a signed URL or proxy later.")

    def delete_object(self, policy, mount, object_key: str, *, hard_delete: bool = False) -> bool:  # type: ignore[override]
        return True

    def presign_put(self, policy, mount, object_key: str) -> PresignedURL:  # type: ignore[override]
        url = self._build_object_url(policy, mount, object_key)
        query = urlencode(
            {
                "signature": self._build_stub_signature("PUT", mount.mount_slug, object_key),
                "expires_in": policy.presign_ttl_seconds,
            }
        )
        return PresignedURL(
            method="PUT",
            url=f"{url}?{query}",
            headers={"X-Cloudrave-Provider": "tencent_cos"},
            expires_at=utcnow() + timedelta(seconds=policy.presign_ttl_seconds),
        )

    def create_multipart_upload(self, policy, mount, object_key: str, *, total_size: int | None = None, mime_type: str | None = None, metadata: dict[str, str] | None = None) -> MultipartUploadState:  # type: ignore[override]
        upload_id = uuid4().hex
        return MultipartUploadState(
            upload_id=upload_id,
            provider_type=self.provider_type,
            mount_id=mount.id,
            object_key=normalize_object_key(object_key),
            upload_root=self.prepare_mount_root(mount),
            upload_dir=self.prepare_mount_root(mount) / ".multipart" / upload_id,
            part_size_bytes=policy.part_size_mb * 1024 * 1024,
            total_size=total_size,
            metadata=dict(metadata or {}),
            expires_at=utcnow() + timedelta(seconds=policy.presign_ttl_seconds),
            provider_upload_id=upload_id,
        )

    def presign_part(self, policy, mount, upload: MultipartUploadState, part_number: int) -> PresignedURL:  # type: ignore[override]
        url = self._build_object_url(policy, mount, upload.object_key)
        query = urlencode(
            {
                "uploadId": upload.provider_upload_id or upload.upload_id,
                "partNumber": part_number,
                "signature": self._build_stub_signature("PUT", mount.mount_slug, upload.object_key, upload.upload_id, part_number),
            }
        )
        return PresignedURL(
            method="PUT",
            url=f"{url}?{query}",
            headers={"X-Cloudrave-Provider": "tencent_cos"},
            expires_at=upload.expires_at,
            upload_id=upload.upload_id,
            part_number=part_number,
        )

    def upload_part(self, policy, mount, upload: MultipartUploadState, part_number: int, body) -> MultipartPart:  # type: ignore[override]
        raise NotImplementedError("Tencent COS parts should be uploaded directly with presigned URLs.")

    def list_parts(self, policy, mount, upload: MultipartUploadState) -> list[MultipartPart]:  # type: ignore[override]
        return []

    def complete_multipart_upload(self, policy, mount, upload: MultipartUploadState, parts: list[MultipartPart], *, mime_type: str | None = None, metadata: dict[str, str] | None = None) -> StorageObject:  # type: ignore[override]
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=mount.id,
            object_key=upload.object_key,
            storage_path=Path.cwd() / "upload" / "cos" / mount.mount_slug / normalize_object_key(upload.object_key),
            size=upload.total_size,
            etag=self._build_stub_signature("POST", mount.mount_slug, upload.object_key, upload.upload_id),
            mime_type=mime_type,
            metadata=dict(metadata or {}),
        )

    def abort_multipart_upload(self, policy, mount, upload: MultipartUploadState) -> bool:  # type: ignore[override]
        return True

    def build_download_url(self, policy, mount, object_key: str, *, expires_in: int | None = None) -> PresignedURL:  # type: ignore[override]
        url = self._build_object_url(policy, mount, object_key)
        query = urlencode(
            {
                "signature": self._build_stub_signature("GET", mount.mount_slug, object_key),
                "expires_in": expires_in or policy.presign_ttl_seconds,
            }
        )
        return PresignedURL(
            method="GET",
            url=f"{url}?{query}",
            headers={},
            expires_at=utcnow() + timedelta(seconds=expires_in or policy.presign_ttl_seconds),
        )

    def configure_cors(self, policy, mount) -> dict[str, object]:  # type: ignore[override]
        return {
            "provider_type": self.provider_type,
            "mount_id": mount.id,
            "status": "stub",
            "message": "Tencent COS CORS signing skeleton is ready; wire SDK/API later.",
        }

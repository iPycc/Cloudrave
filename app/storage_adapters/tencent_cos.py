"""Tencent Cloud COS storage adapter using the official qcloud_cos SDK."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from app.models.tables import BucketMount, StoragePolicy
from app.storage.types import (
    MultipartPart,
    MultipartUploadState,
    PresignedURL,
    StorageCapability,
    StorageObject,
    normalize_object_key,
    utcnow,
)
from config import TencentCosConfig

try:
    from qcloud_cos import CosConfig, CosS3Client

    _COS_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _COS_SDK_AVAILABLE = False


class TencentCosAdapter:
    provider_type = "tencent_cos"
    capabilities = StorageCapability(
        supports_presign=True,
        supports_multipart=True,
        supports_cors_autoconfig=False,
        supports_direct_write=False,
    )

    def __init__(self, config: TencentCosConfig | None = None) -> None:
        self.config = config
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if not _COS_SDK_AVAILABLE:
            raise RuntimeError(
                "Tencent Cloud COS SDK is not installed. "
                "Install it with: pip install cos-python-sdk-v5"
            )
        if self._client is not None:
            return self._client
        if self.config is None:
            raise RuntimeError("Tencent COS config is missing")
        if not self.config.secret_id or not self.config.secret_key or not self.config.bucket:
            raise RuntimeError(
                "Tencent COS credentials are incomplete. "
                "Set secret_id, secret_key and bucket in config.ini [tencent_cos]."
            )
        cos_config = CosConfig(
            Region=self.config.default_region,
            SecretId=self.config.secret_id,
            SecretKey=self.config.secret_key,
            Token=None,
            Scheme="https",
        )
        self._client = CosS3Client(cos_config)
        return self._client

    def _bucket(self) -> str:
        if self.config is None or not self.config.bucket:
            raise RuntimeError("Tencent COS bucket is not configured")
        return self.config.bucket

    def _key(self, mount: BucketMount, object_key: str) -> str:
        root = mount.root_path.strip("/")
        key = normalize_object_key(f"{root}/{object_key}" if root else object_key)
        return key

    def prepare_mount_root(self, mount: BucketMount) -> Path:
        return Path(mount.root_path.strip("/"))

    def put_object(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        object_key: str,
        body: bytes | BinaryIO,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StorageObject:
        client = self._ensure_client()
        key = self._key(mount, object_key)
        kwargs: dict[str, Any] = {}
        if mime_type:
            kwargs["ContentType"] = mime_type
        if metadata:
            kwargs["Metadata"] = metadata
        response = client.put_object(Bucket=self._bucket(), Key=key, Body=body, **kwargs)
        etag = response.get("ETag", "") if isinstance(response, dict) else ""
        size = len(body) if isinstance(body, bytes) else None
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=object_key,
            storage_path=Path(key),
            size=size,
            etag=etag,
            mime_type=mime_type,
            metadata=metadata or {},
        )

    def open_object(self, policy: StoragePolicy, mount: BucketMount, object_key: str) -> BinaryIO:
        client = self._ensure_client()
        key = self._key(mount, object_key)
        response = client.get_object(Bucket=self._bucket(), Key=key)
        return response["Body"]

    def delete_object(self, policy: StoragePolicy, mount: BucketMount, object_key: str, *, hard_delete: bool = False) -> bool:
        client = self._ensure_client()
        key = self._key(mount, object_key)
        client.delete_object(Bucket=self._bucket(), Key=key)
        return True

    def presign_put(self, policy: StoragePolicy, mount: BucketMount, object_key: str) -> PresignedURL:
        client = self._ensure_client()
        key = self._key(mount, object_key)
        ttl = self.config.presign_ttl_seconds if self.config else 900
        url = client.get_presigned_url(
            Method="PUT",
            Bucket=self._bucket(),
            Key=key,
            Expired=ttl,
        )
        return PresignedURL(
            method="PUT",
            url=url,
            expires_at=utcnow() + timedelta(seconds=ttl),
        )

    def create_multipart_upload(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        object_key: str,
        *,
        total_size: int | None = None,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MultipartUploadState:
        client = self._ensure_client()
        key = self._key(mount, object_key)
        kwargs: dict[str, Any] = {}
        if mime_type:
            kwargs["ContentType"] = mime_type
        if metadata:
            kwargs["Metadata"] = metadata
        response = client.create_multipart_upload(Bucket=self._bucket(), Key=key, **kwargs)
        upload_id = response.get("UploadId", "") if isinstance(response, dict) else ""
        ttl = self.config.presign_ttl_seconds if self.config else 900
        return MultipartUploadState(
            upload_id=upload_id,
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=object_key,
            upload_root=Path(mount.root_path.strip("/")),
            upload_dir=Path(mount.root_path.strip("/")) / ".multipart" / upload_id,
            part_size_bytes=policy.part_size_mb * 1024 * 1024,
            total_size=total_size,
            expires_at=utcnow() + timedelta(seconds=ttl),
            provider_upload_id=upload_id,
        )

    def presign_part(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        upload: MultipartUploadState,
        part_number: int,
    ) -> PresignedURL:
        client = self._ensure_client()
        key = self._key(mount, upload.object_key)
        ttl = self.config.presign_ttl_seconds if self.config else 900
        params = {
            "partNumber": str(part_number),
            "uploadId": upload.upload_id,
        }
        url = client.get_presigned_url(
            Method="PUT",
            Bucket=self._bucket(),
            Key=key,
            Expired=ttl,
            Params=params,
        )
        return PresignedURL(
            method="PUT",
            url=url,
            expires_at=utcnow() + timedelta(seconds=ttl),
            part_number=part_number,
            upload_id=upload.upload_id,
        )

    def upload_part(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        upload: MultipartUploadState,
        part_number: int,
        body: bytes | BinaryIO,
    ) -> MultipartPart:
        client = self._ensure_client()
        key = self._key(mount, upload.object_key)
        response = client.upload_part(
            Bucket=self._bucket(),
            Key=key,
            PartNumber=part_number,
            UploadId=upload.upload_id,
            Body=body,
            ContentLength=len(body) if isinstance(body, bytes) else None,
        )
        etag = response.get("ETag", "") if isinstance(response, dict) else ""
        return MultipartPart(
            part_number=part_number,
            etag=etag,
            size=len(body) if isinstance(body, bytes) else None,
        )

    def list_parts(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        upload: MultipartUploadState,
    ) -> list[MultipartPart]:
        client = self._ensure_client()
        key = self._key(mount, upload.object_key)
        response = client.list_parts(
            Bucket=self._bucket(),
            Key=key,
            UploadId=upload.upload_id,
        )
        parts = response.get("Part", []) if isinstance(response, dict) else []
        if isinstance(parts, dict):
            parts = [parts]
        return [
            MultipartPart(
                part_number=int(part.get("PartNumber", 0)),
                etag=part.get("ETag", ""),
                size=int(part.get("Size", 0)) if "Size" in part else None,
            )
            for part in parts
        ]

    def complete_multipart_upload(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        upload: MultipartUploadState,
        parts: list[MultipartPart],
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StorageObject:
        client = self._ensure_client()
        key = self._key(mount, upload.object_key)
        multipart_upload = {
            "Part": [
                {"PartNumber": part.part_number, "ETag": part.etag}
                for part in sorted(parts, key=lambda p: p.part_number)
            ]
        }
        response = client.complete_multipart_upload(
            Bucket=self._bucket(),
            Key=key,
            UploadId=upload.upload_id,
            MultipartUpload=multipart_upload,
        )
        etag = response.get("ETag", "") if isinstance(response, dict) else ""
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=upload.object_key,
            storage_path=Path(key),
            etag=etag,
            mime_type=mime_type,
            metadata=metadata or {},
        )

    def abort_multipart_upload(self, policy: StoragePolicy, mount: BucketMount, upload: MultipartUploadState) -> bool:
        client = self._ensure_client()
        key = self._key(mount, upload.object_key)
        client.abort_multipart_upload(
            Bucket=self._bucket(),
            Key=key,
            UploadId=upload.upload_id,
        )
        return True

    def build_download_url(
        self,
        policy: StoragePolicy,
        mount: BucketMount,
        object_key: str,
        *,
        expires_in: int | None = None,
    ) -> PresignedURL:
        client = self._ensure_client()
        key = self._key(mount, object_key)
        ttl = expires_in or (self.config.presign_ttl_seconds if self.config else 900)
        url = client.get_presigned_url(
            Method="GET",
            Bucket=self._bucket(),
            Key=key,
            Expired=ttl,
        )
        return PresignedURL(
            method="GET",
            url=url,
            expires_at=utcnow() + timedelta(seconds=ttl),
        )

    def configure_cors(self, policy: StoragePolicy, mount: BucketMount) -> dict[str, Any]:
        return {}

"""Aliyun OSS adapter implemented with the official Python SDK V2."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit

from store.key import logical_key, mount_prefix, physical_key, physical_list_prefix
from store.oss.stream import OssStream
from store.identity import physical_hash
from store.errors import storage_error_code
from store.port import StorageAdapterBase
from store.types import (
    MultipartPart,
    MultipartUploadState,
    PresignedURL,
    StorageCapability,
    StorageObject,
    StorageObjectPage,
    utcnow,
)

try:
    import alibabacloud_oss_v2 as oss

    _OSS_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - deployment guard
    oss = None  # type: ignore[assignment]
    _OSS_SDK_AVAILABLE = False


CLIENT_CACHE_SIZE = 64


class AliyunOssAdapter(StorageAdapterBase):
    provider_type = "aliyun_oss"
    capabilities = StorageCapability(
        supports_presign=True,
        supports_multipart=True,
        supports_cors_autoconfig=True,
        supports_direct_write=False,
        supports_single_put=True,
        supports_range=True,
        supports_mirror=True,
        supports_versioned_delete=True,
        supports_server_side_encryption=True,
        upload_transport="presigned",
        requires_part_etag=True,
        min_part_size=100 * 1024,
        max_part_size=5 * 1024 * 1024 * 1024,
        max_parts=10_000,
        max_object_size=48_800 * 1024 * 1024 * 1024,
    )

    def __init__(self) -> None:
        self._clients: OrderedDict[tuple[str, ...], Any] = OrderedDict()
        self._clients_lock = Lock()

    def _target(self, policy: Any) -> dict[str, str | None]:
        credentials = dict(getattr(policy, "credentials", None) or {})
        bucket = str(getattr(policy, "bucket_name", "") or "").strip()
        region = str(getattr(policy, "region", "") or "cn-hangzhou").strip()
        endpoint = self._endpoint(getattr(policy, "endpoint", None))
        access_key_id = str(credentials.get("access_key_id") or "").strip()
        access_key_secret = str(credentials.get("access_key_secret") or "").strip()
        security_token = str(credentials.get("security_token") or "").strip() or None
        if not bucket:
            raise RuntimeError("Aliyun OSS policy bucket_name is required")
        if not region:
            raise RuntimeError("Aliyun OSS policy region is required")
        if not access_key_id or not access_key_secret:
            raise RuntimeError("Aliyun OSS policy credentials are incomplete")
        return {
            "bucket": bucket,
            "region": region,
            "endpoint": endpoint,
            "access_key_id": access_key_id,
            "access_key_secret": access_key_secret,
            "security_token": security_token,
        }

    def _client_for(self, policy: Any) -> Any:
        if not _OSS_SDK_AVAILABLE:
            raise RuntimeError("Aliyun OSS SDK V2 is not installed; install alibabacloud-oss-v2")
        target = self._target(policy)
        credential_hash = sha256(
            f"{target['access_key_id']}\0{target['access_key_secret']}\0{target['security_token'] or ''}".encode()
        ).hexdigest()
        cache_key = (
            str(target["region"]), str(target["endpoint"] or ""), credential_hash
        )
        with self._clients_lock:
            client = self._clients.get(cache_key)
            if client is None:
                config = oss.config.load_default()
                config.credentials_provider = oss.credentials.StaticCredentialsProvider(
                    str(target["access_key_id"]),
                    str(target["access_key_secret"]),
                    target["security_token"],
                )
                config.region = str(target["region"])
                if target["endpoint"]:
                    config.endpoint = str(target["endpoint"])
                client = oss.Client(config)
                self._clients[cache_key] = client
                if len(self._clients) > CLIENT_CACHE_SIZE:
                    self._clients.popitem(last=False)
            else:
                self._clients.move_to_end(cache_key)
        return client

    def prepare_mount_root(self, mount: Any) -> Path:
        return Path(str(getattr(mount, "root_path", "")).strip("/\\"))

    def physical_id(self, policy: Any, mount: Any, object_key: str) -> str:
        return physical_hash(
            self.provider_type,
            self._bucket(policy).lower(),
            self._key(policy, mount, object_key),
        )

    def validate_mount(self, policy: Any, mount: Any) -> dict[str, Any]:
        client = self._client_for(policy)
        result = client.get_bucket_info(oss.GetBucketInfoRequest(bucket=self._bucket(policy)))
        sample = self.list_objects(policy, mount, max_keys=1)
        return {
            "bucket": self._bucket(policy),
            "region": self._region(policy),
            "prefix": self._mount_prefix(policy, mount),
            "request_id": getattr(result, "request_id", None),
            "sample_count": len(sample.objects),
            "has_more": sample.is_truncated,
        }

    def list_objects(
        self,
        policy: Any,
        mount: Any,
        *,
        prefix: str = "",
        marker: str | None = None,
        max_keys: int = 1000,
        delimiter: str | None = None,
    ) -> StorageObjectPage:
        result = self._client_for(policy).list_objects_v2(oss.ListObjectsV2Request(
            bucket=self._bucket(policy),
            prefix=self._list_prefix(policy, mount, prefix),
            continuation_token=marker,
            max_keys=max(1, min(int(max_keys), 1000)),
            delimiter=delimiter,
        ))
        objects = [
            StorageObject(
                provider_type=self.provider_type,
                mount_id=str(mount.id),
                object_key=logical_key(policy, mount, item.key),
                storage_path=Path(item.key),
                size=getattr(item, "size", None),
                etag=self._clean_etag(getattr(item, "etag", None)),
                metadata={
                    "last_modified": str(getattr(item, "last_modified", "") or ""),
                    "storage_class": str(getattr(item, "storage_class", "") or ""),
                },
            )
            for item in (getattr(result, "contents", None) or [])
        ]
        prefixes = [
            logical_key(policy, mount, item.prefix.rstrip("/")) + "/"
            for item in (getattr(result, "common_prefixes", None) or [])
        ]
        return StorageObjectPage(
            objects=objects,
            common_prefixes=prefixes,
            next_marker=getattr(result, "next_continuation_token", None),
            is_truncated=bool(getattr(result, "is_truncated", False)),
        )

    def iter_objects(self, policy: Any, mount: Any, **kwargs: Any) -> Iterator[StorageObject]:
        marker = kwargs.pop("marker", None)
        while True:
            page = self.list_objects(policy, mount, marker=marker, **kwargs)
            yield from page.objects
            if not page.is_truncated:
                return
            if not page.next_marker or page.next_marker == marker:
                raise RuntimeError("Aliyun OSS returned a truncated object page without a continuation token")
            marker = page.next_marker

    def head_object(self, policy: Any, mount: Any, object_key: str, **_: Any) -> StorageObject:
        result = self._client_for(policy).head_object(oss.HeadObjectRequest(
            bucket=self._bucket(policy), key=self._key(policy, mount, object_key)
        ))
        return self._object_from_result(policy, mount, object_key, result)

    def put_object(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        body: bytes | BinaryIO,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StorageObject:
        result = self._client_for(policy).put_object(oss.PutObjectRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, object_key),
            body=body,
            content_type=mime_type,
            metadata=metadata or None,
            **self._encryption_args(policy),
        ))
        stored = self.head_object(policy, mount, object_key)
        stored.etag = stored.etag or self._clean_etag(getattr(result, "etag", None))
        return stored

    def open_object(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        byte_range: str | tuple[int, int | None] | None = None,
    ) -> BinaryIO:
        result = self._client_for(policy).get_object(oss.GetObjectRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, object_key),
            range_header=self._range_header(byte_range),
        ))
        return OssStream(result.body)

    def copy_object(self, policy: Any, mount: Any, source_key: str, destination_key: str) -> StorageObject:
        self._client_for(policy).copy_object(oss.CopyObjectRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, destination_key),
            source_bucket=self._bucket(policy),
            source_key=self._key(policy, mount, source_key),
            **self._encryption_args(policy),
        ))
        return self.head_object(policy, mount, destination_key)

    def delete_object(self, policy: Any, mount: Any, object_key: str, *, hard_delete: bool = False) -> bool:
        client = self._client_for(policy)
        bucket = self._bucket(policy)
        key = self._key(policy, mount, object_key)
        client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=key))
        if hard_delete:
            paginator = client.list_object_versions_paginator()
            for page in paginator.iter_page(oss.ListObjectVersionsRequest(bucket=bucket, prefix=key)):
                for item in [*(getattr(page, "version", None) or []), *(getattr(page, "delete_marker", None) or [])]:
                    if item.key == key:
                        client.delete_object(oss.DeleteObjectRequest(
                            bucket=bucket, key=key, version_id=item.version_id
                        ))
        return True

    def mount_has_objects(self, policy: Any, mount: Any) -> bool:
        return bool(self.list_objects(policy, mount, max_keys=1).objects)

    def purge_mount(self, policy: Any, mount: Any) -> int:
        client = self._client_for(policy)
        bucket = self._bucket(policy)
        prefix = self._list_prefix(policy, mount)
        deleted = 0
        paginator = client.list_object_versions_paginator()
        for page in paginator.iter_page(oss.ListObjectVersionsRequest(bucket=bucket, prefix=prefix)):
            for item in [*(getattr(page, "version", None) or []), *(getattr(page, "delete_marker", None) or [])]:
                client.delete_object(oss.DeleteObjectRequest(
                    bucket=bucket, key=item.key, version_id=item.version_id
                ))
                deleted += 1
        upload_paginator = client.list_multipart_uploads_paginator()
        for page in upload_paginator.iter_page(oss.ListMultipartUploadsRequest(bucket=bucket, prefix=prefix)):
            for upload in getattr(page, "uploads", None) or []:
                client.abort_multipart_upload(oss.AbortMultipartUploadRequest(
                    bucket=bucket, key=upload.key, upload_id=upload.upload_id
                ))
        return deleted

    def presign_put(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> PresignedURL:
        ttl = self._ttl(policy)
        result = self._client_for(policy).presign(oss.PutObjectRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, object_key),
            content_type=mime_type,
            metadata=metadata or None,
            **self._encryption_args(policy),
        ), expires=timedelta(seconds=ttl))
        return self._presigned(result, ttl)

    def create_multipart_upload(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        total_size: int | None = None,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> MultipartUploadState:
        result = self._client_for(policy).initiate_multipart_upload(oss.InitiateMultipartUploadRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, object_key),
            content_type=mime_type,
            metadata=metadata or None,
            **self._encryption_args(policy),
        ))
        upload_id = str(getattr(result, "upload_id", ""))
        if not upload_id:
            raise RuntimeError("Aliyun OSS did not return an upload ID")
        ttl = self._ttl(policy)
        prefix = self._mount_prefix(policy, mount)
        return MultipartUploadState(
            upload_id=upload_id,
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=object_key,
            upload_root=Path(prefix),
            upload_dir=Path(prefix) / ".multipart" / upload_id,
            part_size_bytes=int(policy.part_size_mb) * 1024 * 1024,
            total_size=total_size,
            metadata=metadata or {},
            expires_at=utcnow() + timedelta(seconds=ttl),
            provider_upload_id=upload_id,
        )

    def presign_part(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        part_number: int,
    ) -> PresignedURL:
        self._validate_part_number(part_number)
        ttl = self._ttl(policy)
        result = self._client_for(policy).presign(oss.UploadPartRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, upload.object_key),
            upload_id=upload.upload_id,
            part_number=part_number,
        ), expires=timedelta(seconds=ttl))
        presigned = self._presigned(result, ttl)
        presigned.upload_id = upload.upload_id
        presigned.part_number = part_number
        return presigned

    def upload_part(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        part_number: int,
        body: bytes | BinaryIO,
    ) -> MultipartPart:
        self._validate_part_number(part_number)
        result = self._client_for(policy).upload_part(oss.UploadPartRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, upload.object_key),
            upload_id=upload.upload_id,
            part_number=part_number,
            body=BytesIO(body) if isinstance(body, bytes) else body,
            content_length=len(body) if isinstance(body, bytes) else None,
        ))
        etag = self._clean_etag(getattr(result, "etag", None))
        if not etag:
            raise RuntimeError("Aliyun OSS did not return an ETag for the uploaded part")
        return MultipartPart(part_number=part_number, etag=etag, size=len(body) if isinstance(body, bytes) else None)

    def list_parts(self, policy: Any, mount: Any, upload: MultipartUploadState) -> list[MultipartPart]:
        paginator = self._client_for(policy).list_parts_paginator()
        result: list[MultipartPart] = []
        for page in paginator.iter_page(oss.ListPartsRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, upload.object_key),
            upload_id=upload.upload_id,
        )):
            result.extend(
                MultipartPart(
                    part_number=int(part.part_number),
                    etag=self._clean_etag(part.etag) or "",
                    size=getattr(part, "size", None),
                )
                for part in (getattr(page, "parts", None) or [])
            )
        return result

    def complete_multipart_upload(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        parts: list[MultipartPart],
        **_: Any,
    ) -> StorageObject:
        if not parts:
            raise ValueError("At least one multipart part is required")
        ordered = sorted(parts, key=lambda item: item.part_number)
        if len({item.part_number for item in ordered}) != len(ordered):
            raise ValueError("Multipart part numbers must be unique")
        self._client_for(policy).complete_multipart_upload(oss.CompleteMultipartUploadRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, upload.object_key),
            upload_id=upload.upload_id,
            complete_multipart_upload=oss.CompleteMultipartUpload(parts=[
                oss.UploadPart(part_number=item.part_number, etag=item.etag) for item in ordered
            ]),
        ))
        completed = self.head_object(policy, mount, upload.object_key)
        if upload.total_size is not None and completed.size != upload.total_size:
            raise RuntimeError(
                f"Object storage multipart size mismatch: expected {upload.total_size}, received {completed.size}"
            )
        return completed

    def abort_multipart_upload(self, policy: Any, mount: Any, upload: MultipartUploadState) -> bool:
        self._client_for(policy).abort_multipart_upload(oss.AbortMultipartUploadRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, upload.object_key),
            upload_id=upload.upload_id,
        ))
        return True

    def build_download_url(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        expires_in: int | None = None,
        download_name: str | None = None,
        inline: bool = False,
    ) -> PresignedURL:
        ttl = self._ttl(policy) if expires_in is None else int(expires_in)
        disposition = None
        if download_name:
            kind = "inline" if inline else "attachment"
            disposition = f"{kind}; filename*=UTF-8''{quote(download_name)}"
        result = self._client_for(policy).presign(oss.GetObjectRequest(
            bucket=self._bucket(policy),
            key=self._key(policy, mount, object_key),
            response_content_disposition=disposition,
        ), expires=timedelta(seconds=ttl))
        return self._presigned(result, ttl)

    def configure_cors(self, policy: Any, mount: Any) -> dict[str, Any]:
        _ = mount
        client = self._client_for(policy)
        extra = dict(getattr(policy, "extra", None) or {})
        origins = [str(item).rstrip("/") for item in extra.get("cors_origins", []) if str(item).strip()]
        if not origins:
            raise ValueError("At least one CORS origin is required")
        existing: list[Any] = []
        try:
            current = client.get_bucket_cors(oss.GetBucketCorsRequest(bucket=self._bucket(policy)))
            configuration = getattr(current, "cors_configuration", None)
            existing = list(getattr(configuration, "cors_rules", None) or [])
        except Exception as exc:
            if storage_error_code(exc) not in {"NoSuchCORSConfiguration", "NoSuchCORS"}:
                raise
        rule = oss.CORSRule(
            allowed_origins=origins,
            allowed_methods=["PUT", "GET", "HEAD", "POST", "DELETE"],
            allowed_headers=["*"],
            expose_headers=[
                "ETag", "Content-Length", "Content-Range", "Accept-Ranges",
                "x-oss-request-id", "x-oss-version-id", "x-oss-hash-crc64ecma",
                "x-oss-server-side-encryption", "x-oss-server-side-encryption-key-id",
            ],
            max_age_seconds=int(extra.get("cors_max_age", 600)),
        )
        signature = self._cors_signature(rule)
        merged = [item for item in existing if self._cors_signature(item) != signature]
        merged.append(rule)
        client.put_bucket_cors(oss.PutBucketCorsRequest(
            bucket=self._bucket(policy),
            cors_configuration=oss.CORSConfiguration(cors_rules=merged),
        ))
        return {
            "status": "configured",
            "bucket": self._bucket(policy),
            "origins": origins,
            "rule_count": len(merged),
        }

    def validate_completed_object(self, policy: Any, mount: Any, stored_object: Any, checksum: str | None = None) -> None:
        verified = self.head_object(policy, mount, stored_object.object_key)
        expected = self._encryption_args(policy).get("server_side_encryption")
        actual = verified.metadata.get("server_side_encryption")
        if expected and actual != expected:
            raise RuntimeError("Aliyun OSS did not apply the configured server-side encryption")
        super().validate_completed_object(policy, mount, verified, checksum)

    def _object_from_result(self, policy: Any, mount: Any, object_key: str, result: Any) -> StorageObject:
        metadata = {str(key): str(value) for key, value in (getattr(result, "metadata", None) or {}).items()}
        if getattr(result, "server_side_encryption", None):
            metadata["server_side_encryption"] = str(result.server_side_encryption)
        if getattr(result, "server_side_encryption_key_id", None):
            metadata["server_side_encryption_key_id"] = str(result.server_side_encryption_key_id)
        return StorageObject(
            provider_type=self.provider_type,
            mount_id=str(mount.id),
            object_key=object_key,
            storage_path=Path(self._key(policy, mount, object_key)),
            size=getattr(result, "content_length", None),
            etag=self._clean_etag(getattr(result, "etag", None)),
            version_id=getattr(result, "version_id", None),
            mime_type=getattr(result, "content_type", None),
            metadata=metadata,
        )

    def _encryption_args(self, policy: Any) -> dict[str, str]:
        extra = dict(getattr(policy, "extra", None) or {})
        mode = str(extra.get("server_side_encryption") or "none").lower()
        if mode in {"", "none"}:
            return {}
        if mode in {"sse-oss", "aes256"}:
            return {"server_side_encryption": "AES256"}
        if mode in {"sse-kms", "kms"}:
            result = {"server_side_encryption": "KMS"}
            key_id = str(extra.get("kms_key_id") or "").strip()
            if key_id:
                result["server_side_encryption_key_id"] = key_id
            return result
        raise ValueError("Aliyun OSS server_side_encryption must be none, sse-oss, or sse-kms")

    def _bucket(self, policy: Any) -> str:
        return str(self._target(policy)["bucket"])

    def _region(self, policy: Any) -> str:
        return str(self._target(policy)["region"])

    def _key(self, policy: Any, mount: Any, object_key: str) -> str:
        return physical_key(policy, mount, object_key)

    def _list_prefix(self, policy: Any, mount: Any, prefix: str = "") -> str:
        return physical_list_prefix(policy, mount, prefix)

    def _mount_prefix(self, policy: Any, mount: Any) -> str:
        return mount_prefix(policy, mount)

    @staticmethod
    def _endpoint(value: Any) -> str | None:
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return None
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        if not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Aliyun OSS endpoint must be a host without a path")
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _range_header(value: str | tuple[int, int | None] | None) -> str | None:
        if value is None or isinstance(value, str):
            return value
        start, end = value
        return f"bytes={start}-{'' if end is None else end}"

    @staticmethod
    def _clean_etag(value: Any) -> str | None:
        result = str(value or "").strip().strip('"')
        return result or None

    @staticmethod
    def _cors_signature(rule: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (
            tuple(sorted(getattr(rule, "allowed_origins", None) or [])),
            tuple(sorted(getattr(rule, "allowed_methods", None) or [])),
        )

    @staticmethod
    def _validate_part_number(part_number: int) -> None:
        if not 1 <= part_number <= 10_000:
            raise ValueError("Object storage multipart part_number must be between 1 and 10000")

    @staticmethod
    def _ttl(policy: Any) -> int:
        value = int(getattr(policy, "presign_ttl_seconds", None) or 900)
        if value <= 0:
            raise ValueError("presign_ttl_seconds must be positive")
        return value

    @staticmethod
    def _presigned(result: Any, ttl: int) -> PresignedURL:
        return PresignedURL(
            method=str(getattr(result, "method", "PUT")),
            url=str(result.url),
            headers={str(key): str(value) for key, value in (getattr(result, "signed_headers", None) or {}).items()},
            expires_at=getattr(result, "expiration", None) or utcnow() + timedelta(seconds=ttl),
        )

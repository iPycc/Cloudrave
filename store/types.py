"""Shared storage types and adapter contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, Literal, Protocol, runtime_checkable
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_object_key(value: str) -> str:
    candidate = value.replace("\\", "/").strip()
    candidate = candidate.lstrip("/")
    if not candidate:
        raise ValueError("object key is required")
    parts: list[str] = []
    for part in PurePosixPath(candidate).parts:
        if part == "..":
            raise ValueError("object key cannot contain parent traversal")
        parts.append(part)
    if not parts:
        raise ValueError("object key is required")
    return "/".join(parts)


def join_object_key(*parts: str) -> str:
    return normalize_object_key("/".join(part.strip("/") for part in parts if part))


@dataclass(slots=True)
class StorageCapability:
    supports_presign: bool = True
    supports_multipart: bool = True
    supports_cors_autoconfig: bool = False
    supports_direct_write: bool = False
    supports_single_put: bool = True
    supports_range: bool = True
    supports_mirror: bool = False
    supports_versioned_delete: bool = False
    supports_server_side_encryption: bool = False
    download_transport: Literal["cloudrave", "browser"] = "cloudrave"
    upload_transport: Literal["cloudrave", "presigned"] = "presigned"
    requires_part_etag: bool = True
    supports_part_listing: bool = True
    eager_part_plans: bool = True
    part_checksum_algorithm: Literal["md5"] | None = None
    part_size_alignment: int = 1
    min_part_size: int = 1024 * 1024
    max_part_size: int = 5 * 1024 * 1024 * 1024
    max_parts: int = 10_000
    max_object_size: int = 50 * 1024 * 1024 * 1024 * 1024

    def names(self) -> tuple[str, ...]:
        flags = {
            "presign": self.supports_presign,
            "multipart": self.supports_multipart,
            "cors": self.supports_cors_autoconfig,
            "direct_write": self.supports_direct_write,
            "single_put": self.supports_single_put,
            "range": self.supports_range,
            "mirror": self.supports_mirror,
            "versioned_delete": self.supports_versioned_delete,
            "server_side_encryption": self.supports_server_side_encryption,
        }
        return tuple(name for name, enabled in flags.items() if enabled)


@dataclass(slots=True)
class StorageObject:
    provider_type: str
    mount_id: str
    object_key: str
    storage_path: Path
    size: int | None = None
    etag: str | None = None
    version_id: str | None = None
    mime_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class StorageObjectPage:
    """One provider page, with provider continuation state kept explicit."""

    objects: list[StorageObject] = field(default_factory=list)
    common_prefixes: list[str] = field(default_factory=list)
    next_marker: str | None = None
    is_truncated: bool = False


@dataclass(slots=True)
class PresignedURL:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None
    upload_id: str | None = None
    part_number: int | None = None
    body_mode: Literal["raw", "multipart"] = "raw"
    form_fields: dict[str, str] = field(default_factory=dict)
    file_field: str = "file"
    record_etag: str | None = None
    response_mode: Literal["header", "wopan_json", "opaque", "skip"] = "header"


@dataclass(slots=True)
class MultipartPart:
    part_number: int
    etag: str
    size: int | None = None
    checksum: str | None = None
    local_path: Path | None = None


@dataclass(slots=True)
class MultipartUploadState:
    upload_id: str
    provider_type: str
    mount_id: str
    object_key: str
    upload_root: Path
    upload_dir: Path
    part_size_bytes: int
    total_size: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None
    provider_upload_id: str | None = None


@dataclass(slots=True)
class UploadPlan:
    session_id: str
    object_key: str
    upload_mode: str
    upload_id: str | None
    upload_urls: list[PresignedURL] = field(default_factory=list)
    expires_at: datetime | None = None


@runtime_checkable
class StorageAdapter(Protocol):
    provider_type: str
    capabilities: StorageCapability

    def prepare_mount_root(self, mount: Any) -> Path:
        ...

    def local_path(self, mount: Any, object_key: str) -> Path | None:
        ...

    def physical_id(self, policy: Any, mount: Any, object_key: str) -> str:
        ...

    def validate_mount(self, policy: Any, mount: Any) -> dict[str, Any]:
        ...

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
        ...

    def iter_objects(self, policy: Any, mount: Any, **kwargs: Any) -> Iterator[StorageObject]:
        ...

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
        ...

    def open_object(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        byte_range: str | tuple[int, int | None] | None = None,
    ) -> BinaryIO:
        ...

    def head_object(self, policy: Any, mount: Any, object_key: str) -> StorageObject:
        ...

    def copy_object(
        self,
        policy: Any,
        mount: Any,
        source_key: str,
        destination_key: str,
    ) -> StorageObject:
        ...

    def delete_object(self, policy: Any, mount: Any, object_key: str, *, hard_delete: bool = False) -> bool:
        ...

    def mount_has_objects(self, policy: Any, mount: Any) -> bool:
        ...

    def purge_mount(self, policy: Any, mount: Any) -> int:
        ...

    def presign_put(
        self,
        policy: Any,
        mount: Any,
        object_key: str,
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> PresignedURL:
        ...

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
        ...

    def presign_part(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        part_number: int,
    ) -> PresignedURL:
        ...

    def upload_part(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        part_number: int,
        body: bytes | BinaryIO,
    ) -> MultipartPart:
        ...

    def list_parts(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
    ) -> list[MultipartPart]:
        ...

    def complete_multipart_upload(
        self,
        policy: Any,
        mount: Any,
        upload: MultipartUploadState,
        parts: list[MultipartPart],
        *,
        mime_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StorageObject:
        ...

    def abort_multipart_upload(self, policy: Any, mount: Any, upload: MultipartUploadState) -> bool:
        ...

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
        ...

    def configure_cors(self, policy: Any, mount: Any) -> dict[str, Any]:
        ...

    def validate_completed_object(
        self,
        policy: Any,
        mount: Any,
        stored_object: StorageObject,
        checksum: str | None = None,
    ) -> None:
        ...


def new_upload_id() -> str:
    return uuid4().hex

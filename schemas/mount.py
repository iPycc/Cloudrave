"""Bucket mount schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, AliasPath, Field, field_validator

from drives.ids import provider_id
from schemas.common import CloudraveSchema
from schemas.name import valid_name


def _slug(value: str) -> str:
    normalized = value.strip().replace(" ", "-").replace("\\", "-").replace("/", "-").lower()
    return valid_name(normalized)


class BucketMountBase(CloudraveSchema):
    policy_id: int
    name: str = Field(min_length=1, max_length=128)
    mount_slug: str = Field(min_length=1, max_length=128)
    root_path: str = ""
    provider_label: str | None = Field(default=None, max_length=128)
    is_enabled: bool = True
    quota_bytes: int | None = Field(default=None, ge=0)
    mode: Literal["managed", "mirror"] = "managed"
    read_only: bool = False
    legacy_prefixed_keys: bool = Field(
        default=False,
        validation_alias=AliasChoices("legacy_prefixed_keys", AliasPath("extra", "legacy_prefixed_keys")),
    )
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("root_path")
    @classmethod
    def normalize_root_path(cls, value: str) -> str:
        return value.strip().strip("/")

    @field_validator("mount_slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        return _slug(value)


class BucketMountCreate(BucketMountBase):
    owner_id: int | None = None


class BucketMountUpdate(CloudraveSchema):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    mount_slug: str | None = Field(default=None, min_length=1, max_length=128)
    root_path: str | None = None
    provider_label: str | None = Field(default=None, max_length=128)
    is_enabled: bool | None = None
    quota_bytes: int | None = Field(default=None, ge=0)
    mode: Literal["managed", "mirror"] | None = None
    read_only: bool | None = None
    legacy_prefixed_keys: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("legacy_prefixed_keys", AliasPath("extra", "legacy_prefixed_keys")),
    )
    extra: dict[str, Any] | None = None

    @field_validator("root_path")
    @classmethod
    def normalize_optional_root_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().strip("/")

    @field_validator("mount_slug")
    @classmethod
    def normalize_optional_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _slug(value)


class BucketMountRead(BucketMountBase):
    id: int
    owner_id: int
    created_at: datetime
    updated_at: datetime
    sync_status: str = "never"
    last_sync_at: datetime | None = None
    sync_error: str | None = None
    synced_objects: int = 0
    driver_id: str = ""
    driver_label: str = ""
    driver_kind: str = ""
    driver_capabilities: list[str] = Field(default_factory=list)


class MountUsageRead(CloudraveSchema):
    mount_id: int
    used_bytes: int
    reserved_bytes: int
    quota_bytes: int | None = None


class BucketMountDeletePreview(CloudraveSchema):
    mount_id: int
    mount_name: str
    provider: str
    bucket_name: str
    prefix: str = ""
    file_count: int = 0
    folder_count: int = 0
    active_upload_count: int = 0
    pending_cleanup_count: int = 0
    remote_objects: bool | None = None
    has_contents: bool = False


class BucketMountDeleteResult(CloudraveSchema):
    message: str
    deleted_objects: int = 0


def mount_read(mount, policy=None, storage_manager=None) -> BucketMountRead:
    result = BucketMountRead.model_validate(mount)
    if policy is None:
        return result

    policy_extra = dict(getattr(policy, "extra", None) or {})
    extra = dict(result.extra)
    extra.setdefault("provider_label", result.provider_label)
    driver_id = provider_id(policy.provider)
    compatibility_types = {
        "local_fs": "local",
        "tencent_cos": "tencent",
        "aliyun_oss": "aliyun",
    }
    extra["storage_type"] = compatibility_types.get(driver_id, driver_id)
    extra["bucket_name"] = policy.bucket_name
    extra["region"] = policy.region
    extra["endpoint"] = policy.endpoint
    extra["multipart_threshold_mb"] = policy.multipart_threshold_mb
    extra["part_size_mb"] = policy.part_size_mb
    extra["presign_ttl_seconds"] = policy.presign_ttl_seconds
    extra["cors_status"] = "healthy" if policy.cors_auto_configured else "warning"
    for key in (
        "concurrency",
        "server_side_encryption",
        "kms_key_id",
        "scheme",
        "accelerate",
        "path_style",
    ):
        if key in policy_extra:
            extra[key] = policy_extra[key]
    metadata = {
        "driver_id": driver_id,
        "driver_label": result.provider_label or driver_id,
        "driver_kind": "",
        "driver_capabilities": [],
    }
    if storage_manager is not None:
        definition = storage_manager.definition_for(driver_id)
        adapter = storage_manager.adapters.get(driver_id)
        metadata.update({
            "driver_label": definition.label,
            "driver_kind": definition.kind,
            "driver_capabilities": list(adapter.capabilities.names()) if adapter else [],
        })
    return result.model_copy(update={"extra": extra, **metadata})

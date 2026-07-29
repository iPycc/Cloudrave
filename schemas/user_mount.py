"""User-scoped storage policy and mount schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from schemas.common import CloudraveSchema
from schemas.mount import BucketMountRead, BucketMountUpdate
from schemas.name import valid_name
from schemas.policy import StoragePolicyCreate, StoragePolicyRead, StoragePolicyUpdate


def _mount_slug(value: str) -> str:
    normalized = value.strip().replace(" ", "-").replace("\\", "-").replace("/", "-").lower()
    return valid_name(normalized)


class UserBucketMountCreate(CloudraveSchema):
    """Mount fields a user may set; owner and policy are server-controlled."""

    name: str = Field(min_length=1, max_length=128)
    mount_slug: str = Field(min_length=1, max_length=128)
    root_path: str = ""
    provider_label: str | None = Field(default=None, max_length=128)
    is_enabled: bool = True
    mode: Literal["managed", "mirror"] = "managed"
    read_only: bool = False
    legacy_prefixed_keys: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("root_path")
    @classmethod
    def normalize_root_path(cls, value: str) -> str:
        return value.strip().strip("/")

    @field_validator("mount_slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        return _mount_slug(value)


class UserMountCreate(CloudraveSchema):
    policy: StoragePolicyCreate
    mount: UserBucketMountCreate


class UserMountUpdate(CloudraveSchema):
    policy: StoragePolicyUpdate | None = None
    mount: BucketMountUpdate | None = None


class UserMountRead(CloudraveSchema):
    policy: StoragePolicyRead
    mount: BucketMountRead


class UserMountDeletePreview(CloudraveSchema):
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


class UserMountDeleteResult(CloudraveSchema):
    message: str
    deleted_objects: int = 0
    policy_deleted: bool = False

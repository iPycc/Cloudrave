"""Bucket mount schemas."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from app.schemas.common import CloudraveSchema


class BucketMountBase(CloudraveSchema):
    policy_id: int
    name: str = Field(min_length=1, max_length=128)
    mount_slug: str = Field(min_length=1, max_length=128)
    root_path: str = ""
    provider_label: str | None = Field(default=None, max_length=128)
    is_enabled: bool = True
    quota_bytes: int | None = Field(default=None, ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("root_path")
    @classmethod
    def normalize_root_path(cls, value: str) -> str:
        return value.strip().strip("/")

    @field_validator("mount_slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        return value.strip().replace(" ", "-").replace("\\", "-").replace("/", "-").lower()


class BucketMountCreate(BucketMountBase):
    owner_id: int | None = None


class BucketMountUpdate(CloudraveSchema):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    mount_slug: str | None = Field(default=None, min_length=1, max_length=128)
    root_path: str | None = None
    provider_label: str | None = Field(default=None, max_length=128)
    is_enabled: bool | None = None
    quota_bytes: int | None = Field(default=None, ge=0)
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
        return value.strip().replace(" ", "-").replace("\\", "-").replace("/", "-").lower()


class BucketMountRead(BucketMountBase):
    id: int
    owner_id: int

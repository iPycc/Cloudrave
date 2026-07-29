"""Storage policy schemas."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from models.enums import StoragePolicyType
from schemas.common import CloudraveSchema


class StoragePolicyBase(CloudraveSchema):
    name: str = Field(min_length=1, max_length=128)
    provider: StoragePolicyType
    bucket_name: str = Field(min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    endpoint: str | None = Field(default=None, max_length=255)
    base_prefix: str = ""
    secret_id: str | None = Field(default=None, max_length=255)
    secret_key: str | None = Field(default=None, max_length=255)
    session_token: str | None = Field(default=None, max_length=255)
    status: str = Field(default="active", max_length=32)
    description: str | None = Field(default=None, max_length=512)
    multipart_threshold_mb: int = Field(default=64, ge=1)
    part_size_mb: int = Field(default=16, ge=1, le=5120)
    presign_ttl_seconds: int = Field(default=900, ge=60)
    is_default: bool = False
    cors_auto_configured: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_prefix")
    @classmethod
    def normalize_base_prefix(cls, value: str) -> str:
        return value.strip().strip("/")


class StoragePolicyCreate(StoragePolicyBase):
    pass


class StoragePolicyUpdate(CloudraveSchema):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    bucket_name: str | None = Field(default=None, min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    endpoint: str | None = Field(default=None, max_length=255)
    base_prefix: str | None = None
    secret_id: str | None = Field(default=None, max_length=255)
    secret_key: str | None = Field(default=None, max_length=255)
    session_token: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=512)
    multipart_threshold_mb: int | None = Field(default=None, ge=1)
    part_size_mb: int | None = Field(default=None, ge=1, le=5120)
    presign_ttl_seconds: int | None = Field(default=None, ge=60)
    is_default: bool | None = None
    cors_auto_configured: bool | None = None
    extra: dict[str, Any] | None = None

    @field_validator("base_prefix")
    @classmethod
    def normalize_optional_base_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().strip("/")


class StoragePolicyRead(StoragePolicyBase):
    id: int

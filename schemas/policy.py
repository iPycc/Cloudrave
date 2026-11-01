"""Storage policy schemas with write-only provider credentials."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from drives.ids import provider_id
from schemas.common import CloudraveSchema


class _PolicySettings(CloudraveSchema):
    bucket_name: str = Field(min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    endpoint: str | None = Field(default=None, max_length=255)
    base_prefix: str = ""
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


class StoragePolicyCreate(_PolicySettings):
    name: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    credentials: dict[str, str] | None = Field(default=None, exclude=True, repr=False)
    # Deprecated for one release. StorageManager normalizes these write-only
    # aliases into the encrypted provider-neutral credential payload.
    secret_id: str | None = Field(default=None, max_length=255, exclude=True, repr=False)
    secret_key: str | None = Field(default=None, max_length=255, exclude=True, repr=False)
    session_token: str | None = Field(default=None, max_length=255, exclude=True, repr=False)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return provider_id(value)


class StoragePolicyUpdate(CloudraveSchema):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    bucket_name: str | None = Field(default=None, min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    endpoint: str | None = Field(default=None, max_length=255)
    base_prefix: str | None = None
    status: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=512)
    multipart_threshold_mb: int | None = Field(default=None, ge=1)
    part_size_mb: int | None = Field(default=None, ge=1, le=5120)
    presign_ttl_seconds: int | None = Field(default=None, ge=60)
    is_default: bool | None = None
    cors_auto_configured: bool | None = None
    extra: dict[str, Any] | None = None
    credentials: dict[str, str] | None = Field(default=None, exclude=True, repr=False)
    secret_id: str | None = Field(default=None, max_length=255, exclude=True, repr=False)
    secret_key: str | None = Field(default=None, max_length=255, exclude=True, repr=False)
    session_token: str | None = Field(default=None, max_length=255, exclude=True, repr=False)

    @field_validator("base_prefix")
    @classmethod
    def normalize_optional_base_prefix(cls, value: str | None) -> str | None:
        return value.strip().strip("/") if value is not None else None


class StoragePolicyRead(_PolicySettings):
    id: int
    name: str
    provider: str
    credentials_configured: bool = False
    credential_fields: list[str] = Field(default_factory=list)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return provider_id(value)

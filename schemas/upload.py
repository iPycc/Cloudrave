"""Upload session schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from models.enums import UploadMode, UploadState
from schemas.common import CloudraveSchema
from schemas.name import valid_name
from store.limits import MAX_OBJECT_SIZE, MAX_PART_SIZE
from store.types import normalize_object_key


class UploadSessionCreate(CloudraveSchema):
    mount_id: int
    parent_id: int | None = None
    file_name: str = Field(min_length=1, max_length=255)
    relative_path: str | None = Field(default=None, max_length=2048)
    checksum: str | None = Field(default=None, max_length=128)
    object_key: str | None = None
    mode: UploadMode = UploadMode.AUTO
    size: int = Field(ge=0, le=MAX_OBJECT_SIZE)
    part_size: int | None = Field(default=None, ge=1, le=MAX_PART_SIZE)
    content_type: str | None = Field(default=None, max_length=255)

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        return valid_name(value)

    @field_validator("object_key")
    @classmethod
    def normalize_optional_object_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_object_key(value)

    @field_validator("relative_path")
    @classmethod
    def normalize_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace("\\", "/").strip("/")
        if not normalized:
            return None
        segments = normalized.split("/")
        if any(not segment for segment in segments):
            raise ValueError("Invalid empty relative path segment")
        return "/".join(valid_name(segment) for segment in segments)


class UploadPartPlan(CloudraveSchema):
    part_number: int
    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime | None = None


class UploadSessionPlan(CloudraveSchema):
    session_id: str
    upload_mode: UploadMode
    upload_id: str | None = None
    object_key: str
    part_size: int
    part_count: int = 1
    upload_urls: list[UploadPartPlan] = Field(default_factory=list)
    expires_at: datetime | None = None
    is_duplicate: bool = False
    node_id: int | None = None


class UploadPartComplete(CloudraveSchema):
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=255)
    size: int = Field(default=0, ge=0)


class UploadPartRead(CloudraveSchema):
    id: int
    part_number: int
    etag: str | None
    size: int
    uploaded_at: datetime


class UploadSessionRead(CloudraveSchema):
    id: str
    mount_id: int
    node_id: int | None
    path: str
    mode: UploadMode
    state: UploadState
    upload_id: str | None
    file_name: str
    content_type: str | None
    checksum: str | None = None
    size: int
    part_size: int
    created_at: datetime
    updated_at: datetime
    parts: list[UploadPartRead] = Field(default_factory=list)


class UploadSessionCompleteRequest(CloudraveSchema):
    parts: list[UploadPartComplete] = Field(default_factory=list)


class UploadSessionAbortRequest(CloudraveSchema):
    reason: str | None = Field(default=None, max_length=255)

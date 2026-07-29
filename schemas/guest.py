"""Administrator-managed guest account schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.common import CloudraveSchema


class GuestCreateRequest(CloudraveSchema):
    source_mount_id: int
    quota_bytes: int = Field(ge=1)
    expires_at: datetime | None = None


class GuestUpdateRequest(CloudraveSchema):
    enabled: bool | None = None
    quota_bytes: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    clear_expiry: bool = False


class GuestCredentialResponse(CloudraveSchema):
    email: str
    password: str


class GuestRead(CloudraveSchema):
    id: int
    uid: str
    email: str
    username: str
    enabled: bool
    expired: bool
    expires_at: datetime | None = None
    source_mount_id: int
    workspace_mount_id: int
    workspace_prefix: str
    quota_bytes: int
    used_bytes: int
    reserved_bytes: int
    created_at: datetime


class GuestCreateResponse(CloudraveSchema):
    guest: GuestRead
    credentials: GuestCredentialResponse

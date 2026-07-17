"""Share link schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.enums import ShareAccess
from app.schemas.common import CloudraveSchema


class ShareCreateRequest(CloudraveSchema):
    node_id: int
    access: ShareAccess = ShareAccess.PUBLIC
    password: str | None = Field(default=None, min_length=1, max_length=128)
    expires_in_hours: int | None = Field(default=None, ge=1, le=720)
    max_downloads: int | None = Field(default=None, ge=1, le=100000)


class ShareRead(CloudraveSchema):
    id: str
    node_id: int
    access: ShareAccess
    expires_at: datetime | None
    max_downloads: int | None
    download_count: int
    view_count: int
    is_active: bool
    created_at: datetime
    node_name: str | None = None
    node_type: str | None = None
    node_size: int | None = None


class ShareVerifyRequest(CloudraveSchema):
    password: str | None = None


class ShareAccessTokenResponse(CloudraveSchema):
    access_token: str


class ShareNodeInfo(CloudraveSchema):
    share_id: str
    access: ShareAccess
    node_id: int
    node_name: str
    node_type: str
    expires_at: datetime | None
    max_downloads: int | None
    download_count: int
    view_count: int

"""Share link schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from app.models.enums import ShareAccess
from app.schemas.common import CloudraveSchema


class ShareCreateRequest(CloudraveSchema):
    node_id: int | None = None
    node_ids: list[int] = Field(default_factory=list, max_length=100)
    access: ShareAccess = ShareAccess.PUBLIC
    password: str | None = Field(default=None, min_length=1, max_length=128)
    expires_in_hours: int | None = Field(default=None, ge=1, le=720)
    max_downloads: int | None = Field(default=None, ge=1, le=100000)

    @model_validator(mode="after")
    def require_password(self) -> "ShareCreateRequest":
        if self.access == ShareAccess.PASSWORD and not self.password:
            raise ValueError("Password-protected shares require a password")
        return self


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
    item_count: int = 1


class ShareVerifyRequest(CloudraveSchema):
    password: str | None = None


class ShareAccessTokenResponse(CloudraveSchema):
    access_token: str


class SharedNodeRead(CloudraveSchema):
    id: int
    parent_id: int | None
    name: str
    type: str
    size: int


class ShareNodeInfo(CloudraveSchema):
    share_id: str
    access: ShareAccess
    node_id: int | None = None
    node_name: str = ""
    node_type: str = ""
    node_size: int = 0
    owner_name: str
    owner_uid: str
    owner_avatar: str | None = None
    folder_count: int = 0
    file_count: int = 0
    items: list[SharedNodeRead] = Field(default_factory=list)
    expires_at: datetime | None
    max_downloads: int | None
    download_count: int
    view_count: int

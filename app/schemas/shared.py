"""Mounted share schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.enums import ShareAccess
from app.schemas.common import CloudraveSchema


class SharedCreate(CloudraveSchema):
    share_id: str = Field(min_length=1, max_length=36)
    access_token: str | None = Field(default=None, max_length=512)


class SharedSaveRequest(CloudraveSchema):
    node_id: int
    target_mount_id: int
    target_parent_id: int | None = None


class SharedNode(CloudraveSchema):
    id: int
    parent_id: int | None
    name: str
    type: str
    size: int


class SharedRead(CloudraveSchema):
    id: int
    share_id: str
    access: ShareAccess | None = None
    owner_id: int | None = None
    owner_username: str | None = None
    owner_avatar: str | None = None
    available: bool
    unavailable_reason: str | None = None
    roots: list[SharedNode] = Field(default_factory=list)
    detail_url: str
    nodes_url: str
    created_at: datetime

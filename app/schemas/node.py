"""Virtual file node schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.models.enums import NodeStatus, NodeType
from app.schemas.common import CloudraveSchema
from app.schemas.name import valid_name


class NodeRead(CloudraveSchema):
    id: int
    owner_id: int
    mount_id: int
    parent_id: int | None
    type: NodeType
    status: NodeStatus
    name: str
    blob_path: str | None
    size: int
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


NodeCategory = Literal["image", "video", "audio", "document"]
NodeSort = Literal["updated-desc", "updated-asc", "name-asc", "name-desc", "size-desc"]


class NodePageRead(CloudraveSchema):
    items: list[NodeRead]
    next_cursor: str | None = None


class NodeSearchResult(CloudraveSchema):
    node: NodeRead
    parent_path: str


class NodeActivityRead(CloudraveSchema):
    id: int
    action: str
    actor_id: int | None = None
    actor_name: str
    actor_avatar: str | None = None
    detail: str | None = None
    created_at: datetime


class FolderCreateRequest(CloudraveSchema):
    mount_id: int
    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=255)

    _valid_name = field_validator("name")(valid_name)


class FileCreateRequest(CloudraveSchema):
    mount_id: int
    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=255)

    _valid_name = field_validator("name")(valid_name)


class NodeRenameRequest(CloudraveSchema):
    name: str = Field(min_length=1, max_length=255)

    _valid_name = field_validator("name")(valid_name)


class NodeMoveRequest(CloudraveSchema):
    node_ids: list[int] = Field(min_length=1)
    target_parent_id: int | None = None


class NodeCopyRequest(CloudraveSchema):
    node_ids: list[int] = Field(min_length=1)
    target_parent_id: int | None = None


class NodeDeleteRequest(CloudraveSchema):
    node_ids: list[int] = Field(min_length=1)
    hard_delete: bool = False


class NodeRestoreRequest(CloudraveSchema):
    node_ids: list[int] = Field(min_length=1)


class DownloadLinkResponse(CloudraveSchema):
    method: str
    url: str
    expires_at: datetime | None = None

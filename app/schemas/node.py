"""Virtual file node schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.enums import NodeStatus, NodeType
from app.schemas.common import CloudraveSchema


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


class FolderCreateRequest(CloudraveSchema):
    mount_id: int
    parent_id: int | None = None
    name: str = Field(min_length=1, max_length=255)


class NodeRenameRequest(CloudraveSchema):
    name: str = Field(min_length=1, max_length=255)


class NodeDeleteRequest(CloudraveSchema):
    node_ids: list[int] = Field(min_length=1)
    hard_delete: bool = False


class NodeRestoreRequest(CloudraveSchema):
    node_ids: list[int] = Field(min_length=1)


class DownloadLinkResponse(CloudraveSchema):
    method: str
    url: str
    expires_at: datetime | None = None

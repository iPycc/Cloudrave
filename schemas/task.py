"""User-visible background task schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from schemas.common import CloudraveSchema


class ArchiveExtractRequest(CloudraveSchema):
    target_parent_id: int | None = None
    paths: list[str] | None = Field(default=None, max_length=500)


class BackgroundTaskRead(CloudraveSchema):
    id: str
    owner_id: int
    kind: Literal["archive_extract"]
    name: str
    status: Literal["pending", "running", "completed", "failed"]
    progress: int = Field(ge=0, le=100)
    detail: str | None = None
    result_node_id: int | None = None
    created_at: datetime
    updated_at: datetime

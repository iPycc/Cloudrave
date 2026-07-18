"""Bucket mirror synchronization schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import CloudraveSchema


class MountSyncRequest(CloudraveSchema):
    page_size: int = Field(default=500, ge=1, le=1000)
    max_pages: int = Field(default=1, ge=1, le=1)
    restart: bool = False


class MountSyncRead(CloudraveSchema):
    mount_id: int
    mode: str
    status: str
    generation: str | None = None
    cursor: str | None = None
    complete: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    scanned: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    conflicts: int = 0
    missing: int = 0
    synced_objects: int = 0
    error: str | None = None

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


class MountSyncTaskRead(CloudraveSchema):
    id: str
    mount_id: int
    owner_id: int
    status: Literal["pending", "running", "paused", "completed", "failed", "canceled"]
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    name: str | None = None
    scanned: int = 0
    created: int = 0
    updated: int = 0
    conflicts: int = 0
    skipped: int = 0
    missing: int = 0


class MountSyncConflictRead(CloudraveSchema):
    id: int
    key: str
    reason: Literal["invalid_path", "folder_conflict", "source_unavailable", "indexed_elsewhere", "name_conflict"]


class MountSyncConflictPage(CloudraveSchema):
    items: list[MountSyncConflictRead]
    next_after_id: int | None = None


class PathPlanRequest(CloudraveSchema):
    template: str = Field(min_length=1, max_length=1024)


class PathMigrationRead(CloudraveSchema):
    id: str
    status: Literal["pending", "running", "paused", "completed", "failed", "canceled"]
    migrated: int
    prepared: int = 0
    conflicts: int
    error: str | None = None


class PathPlanRead(CloudraveSchema):
    id: str
    mount_id: int
    owner_id: int
    template: str
    rename_to: str | None = None
    move_to: int | None = None
    copy_to: int | None = None
    copy_name: str | None = None
    root_node_id: int | None = None
    source_name: str | None = None
    status: Literal["pending", "running", "paused", "completed", "failed", "canceled"]
    scanned: int
    ready: int
    unchanged: int
    conflicts: int
    bytes_to_copy: int
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    name: str | None = None
    migration: PathMigrationRead | None = None


class PathChangeQueued(CloudraveSchema):
    kind: Literal["path_change"] = "path_change"
    task_id: str


class PathBatchQueued(CloudraveSchema):
    kind: Literal["path_change"] = "path_change"
    task_ids: list[str]


class PathPlanConflictRead(CloudraveSchema):
    id: int
    node_id: int
    source: str | None
    target: str | None
    reason: str


class PathPlanConflictPage(CloudraveSchema):
    items: list[PathPlanConflictRead]
    next_after_id: int | None = None

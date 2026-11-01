"""Database administration and cross-backend migration schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from core.cfg import DatabaseBackend
from schemas.common import CloudraveSchema


class DatabaseProfileRead(CloudraveSchema):
    backend: DatabaseBackend
    host: str | None = None
    port: int | None = None
    user: str | None = None
    name: str
    password_configured: bool = False


class DatabaseTableSummary(CloudraveSchema):
    table: str
    rows: int


class DatabaseDataSummary(CloudraveSchema):
    total_rows: int = 0
    has_data: bool = False
    tables: list[DatabaseTableSummary] = Field(default_factory=list)


class DatabaseConnectionRead(CloudraveSchema):
    reachable: bool
    backend: DatabaseBackend
    server_version: str | None = None
    schema_version: int | None = None
    instance_id: str | None = None
    summary: DatabaseDataSummary = Field(default_factory=DatabaseDataSummary)
    database_created: bool = False
    error_code: Literal[
        "database_not_found",
        "database_creation_failed",
        "authentication_failed",
        "permission_denied",
        "connection_failed",
        "unknown",
    ] | None = None
    error: str | None = None


class DatabaseMigrationLogRead(CloudraveSchema):
    seq: int
    timestamp: datetime
    level: Literal["info", "warning", "error"]
    message: str


DatabaseMigrationPhase = Literal[
    "queued",
    "connecting",
    "inventory",
    "backup",
    "schema",
    "copying",
    "verifying",
    "ready",
    "failed",
]


class DatabaseMigrationRead(CloudraveSchema):
    id: str
    source_backend: DatabaseBackend
    target_backend: DatabaseBackend
    data_choice: Literal["source", "target"]
    phase: DatabaseMigrationPhase
    progress: int = Field(ge=0, le=100)
    current_table: str | None = None
    processed_rows: int = 0
    total_rows: int = 0
    detail: str = ""
    restart_required: bool = False
    backup_available: bool = False
    backup_name: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    logs: list[DatabaseMigrationLogRead] = Field(default_factory=list)


class DatabaseStatusRead(CloudraveSchema):
    managed_by: Literal["config", "url", "environment"]
    editable: bool
    config_path: str
    active: DatabaseProfileRead
    requested: DatabaseProfileRead
    profiles: list[DatabaseProfileRead] = Field(default_factory=list)
    switch_pending: bool
    activation_pending: bool
    activation_failed: bool
    source: DatabaseConnectionRead | None = None
    target: DatabaseConnectionRead | None = None
    current_migration: DatabaseMigrationRead | None = None
    recent_migrations: list[DatabaseMigrationRead] = Field(default_factory=list)
    warning: str | None = None


class DatabaseConfigUpdate(CloudraveSchema):
    backend: DatabaseBackend
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=1024)
    name: str | None = Field(default=None, max_length=255)
    sqlite_path: str | None = Field(default=None, max_length=2048)


class DatabaseConnectionTestRequest(DatabaseConfigUpdate):
    pass


class DatabaseMigrationStartRequest(CloudraveSchema):
    data_choice: Literal["source", "target"]
    current_password: str = Field(min_length=1, max_length=1024)
    confirmation: str = Field(min_length=1, max_length=255)

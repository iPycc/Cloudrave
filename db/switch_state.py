"""Persistent database switch state kept outside every database backend."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from core.cfg import CloudraveConfig, DatabaseBackend, SUPPORTED_DATABASES, database_config_for_backend


SwitchStatus = Literal[
    "active",
    "pending",
    "migrating",
    "activation_pending",
    "activation_failed",
]


@dataclass(slots=True)
class DatabaseSwitchState:
    instance_id: str
    active_backend: DatabaseBackend
    active_fingerprint: str
    status: SwitchStatus = "active"
    requested_backend: DatabaseBackend | None = None
    migration_id: str | None = None
    last_error: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def state_path(root_dir: Path) -> Path:
    return root_dir / "data" / "database-state.json"


def database_fingerprint(config: CloudraveConfig) -> str:
    return hashlib.sha256(config.database.url.encode("utf-8")).hexdigest()


def load_switch_state(config: CloudraveConfig) -> DatabaseSwitchState:
    path = state_path(config.root_dir)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = DatabaseSwitchState(**raw)
            if loaded.active_backend not in SUPPORTED_DATABASES:
                raise ValueError("invalid active database backend")
            if loaded.requested_backend is not None and loaded.requested_backend not in SUPPORTED_DATABASES:
                raise ValueError("invalid requested database backend")
            if loaded.status not in {
                "active", "pending", "migrating", "activation_pending", "activation_failed"
            }:
                raise ValueError("invalid database switch status")
            return loaded
        except (OSError, ValueError, TypeError):
            # A corrupt sidecar must not prevent the application from starting.
            # It is replaced after the current database becomes ready.
            pass

    active_config = _infer_initial_active_config(config)
    return DatabaseSwitchState(
        instance_id=str(uuid4()),
        active_backend=active_config.database.backend,
        active_fingerprint=database_fingerprint(active_config),
        status=(
            "pending"
            if active_config.database.backend != config.database.backend
            else "active"
        ),
        requested_backend=(
            config.database.backend
            if active_config.database.backend != config.database.backend
            else None
        ),
    )


def save_switch_state(root_dir: Path, state: DatabaseSwitchState) -> Path:
    path = state_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = datetime.now(UTC).isoformat()
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    return path


def runtime_config(config: CloudraveConfig, state: DatabaseSwitchState) -> CloudraveConfig:
    if state.status == "activation_pending" and state.requested_backend:
        return database_config_for_backend(config, state.requested_backend)
    return database_config_for_backend(config, state.active_backend)


def _infer_initial_active_config(config: CloudraveConfig) -> CloudraveConfig:
    if config.database.backend == "sqlite":
        return config

    sqlite_config = database_config_for_backend(config, "sqlite")
    sqlite_path = Path(sqlite_config.database.url.removeprefix("sqlite+aiosqlite:///"))
    if not sqlite_path.is_file() or sqlite_path.stat().st_size == 0:
        return config

    # Cover the common upgrade path where database= was changed before the new
    # sidecar had a chance to record SQLite as the active source.
    import sqlite3

    try:
        connection = sqlite3.connect(sqlite_path)
        try:
            users = connection.execute(
                "SELECT count(*) FROM users"
            ).fetchone()
            if users and int(users[0]) > 0:
                return sqlite_config
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    return config

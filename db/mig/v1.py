"""Migration 1: preserve and version the legacy runtime schema repairs."""

from __future__ import annotations

import secrets
import string

from sqlalchemy import BigInteger, Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    _large_sizes(connection, inspector, tables)
    if "users" in tables:
        _users(connection, inspector)
    if "objects" in tables:
        _objects(connection, inspector)
    if "upload_sessions" in tables:
        _uploads(connection, inspector)
    if "share_mounts" in tables:
        _shared(connection, inspector)


def _users(connection: Connection, inspector) -> None:
    columns = {column["name"] for column in inspector.get_columns("users")}
    dialect = connection.dialect.name

    if "uid" not in columns:
        connection.execute(text(f"ALTER TABLE users ADD COLUMN uid {_varchar(dialect, 16)}"))
    if "avatar_path" not in columns:
        connection.execute(
            text(f"ALTER TABLE users ADD COLUMN avatar_path {_varchar(dialect, 512)}")
        )
    if "timezone" not in columns:
        connection.execute(
            text(f"ALTER TABLE users ADD COLUMN timezone {_varchar(dialect, 64)}")
        )
    if "password_updated_at" not in columns:
        connection.execute(
            text(f"ALTER TABLE users ADD COLUMN password_updated_at {_datetime(dialect)}")
        )
    if "totp_secret" not in columns:
        connection.execute(
            text(f"ALTER TABLE users ADD COLUMN totp_secret {_varchar(dialect, 255)}")
        )
    if "totp_enabled" not in columns:
        connection.execute(
            text("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT 0")
        )
    if "totp_backup_codes" not in columns:
        connection.execute(
            text("ALTER TABLE users ADD COLUMN totp_backup_codes TEXT DEFAULT '[]'")
        )

    rows = connection.execute(
        text(
            "SELECT id, created_at, uid, timezone, password_updated_at "
            "FROM users ORDER BY id ASC"
        )
    ).mappings()
    used_uids: set[str] = set()

    for row in rows:
        current_uid = row.get("uid")
        updates: dict[str, object] = {}

        if not current_uid:
            candidate = _uid(used_uids)
            updates["uid"] = candidate
            used_uids.add(candidate)
        else:
            used_uids.add(str(current_uid))

        if row.get("password_updated_at") is None:
            updates["password_updated_at"] = row.get("created_at")
        if not row.get("timezone"):
            updates["timezone"] = "UTC"

        if updates:
            updates["id"] = row["id"]
            connection.execute(
                text(
                    "UPDATE users SET "
                    + ", ".join(
                        f"{column} = :{column}"
                        for column in updates
                        if column != "id"
                    )
                    + " WHERE id = :id"
                ),
                updates,
            )

    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_uid" not in indexes:
        connection.execute(text("CREATE UNIQUE INDEX ix_users_uid ON users (uid)"))


def _large_sizes(connection: Connection, inspector, tables: set[str]) -> None:
    dialect = connection.dialect.name
    if dialect == "sqlite":
        return

    targets = {
        "bucket_mounts": {"quota_bytes": True},
        "nodes": {"size": False},
        "upload_sessions": {"size": False, "part_size": False},
        "upload_parts": {"size": False},
    }
    for table, columns in targets.items():
        if table not in tables:
            continue
        existing = {
            column["name"]: column
            for column in inspector.get_columns(table)
        }
        for name, nullable in columns.items():
            column = existing.get(name)
            if column is None or isinstance(column["type"], BigInteger):
                continue
            if dialect == "postgresql":
                statement = f"ALTER TABLE {table} ALTER COLUMN {name} TYPE BIGINT"
            elif dialect == "mysql":
                nullability = "NULL" if nullable else "NOT NULL"
                statement = (
                    f"ALTER TABLE {table} MODIFY COLUMN {name} BIGINT {nullability}"
                )
            else:
                continue
            connection.execute(text(statement))


def _objects(connection: Connection, inspector) -> None:
    columns = {column["name"] for column in inspector.get_columns("objects")}
    if "physical_id" not in columns:
        connection.execute(
            text(
                "ALTER TABLE objects ADD COLUMN physical_id "
                f"{_varchar(connection.dialect.name, 64)}"
            )
        )
    indexes = {
        index["name"]
        for index in inspect(connection).get_indexes("objects")
    }
    if "ix_objects_physical_id" not in indexes:
        connection.execute(
            text("CREATE INDEX ix_objects_physical_id ON objects (physical_id)")
        )


def _shared(connection: Connection, inspector) -> None:
    columns = {
        column["name"]
        for column in inspector.get_columns("share_mounts")
    }
    if "grant_hash" not in columns:
        connection.execute(
            text(
                "ALTER TABLE share_mounts ADD COLUMN grant_hash "
                f"{_varchar(connection.dialect.name, 64)}"
            )
        )


def _uploads(connection: Connection, inspector) -> None:
    columns = {
        column["name"]
        for column in inspector.get_columns("upload_sessions")
    }
    if "checksum" not in columns:
        connection.execute(
            text(
                "ALTER TABLE upload_sessions ADD COLUMN checksum "
                f"{_varchar(connection.dialect.name, 128)}"
            )
        )


def _varchar(_dialect: str, length: int) -> str:
    return f"VARCHAR({length})"


def _datetime(dialect: str) -> str:
    if dialect == "postgresql":
        return "TIMESTAMP WITH TIME ZONE"
    return "DATETIME"


def _uid(used_uids: set[str]) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(5))
        if candidate not in used_uids:
            return candidate

"""Migration 2: persist the user's file thumbnail preference."""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "thumbnails_enabled" not in columns:
        connection.execute(
            text("ALTER TABLE users ADD COLUMN thumbnails_enabled BOOLEAN DEFAULT TRUE")
        )
    connection.execute(
        text("UPDATE users SET thumbnails_enabled = TRUE WHERE thumbnails_enabled IS NULL")
    )

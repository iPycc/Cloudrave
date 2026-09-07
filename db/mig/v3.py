"""Migration 3: persist the provider avatar URL for OAuth identities."""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if "oauth_identities" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("oauth_identities")}
    if "provider_avatar_url" not in columns:
        connection.execute(
            text("ALTER TABLE oauth_identities ADD COLUMN provider_avatar_url VARCHAR(1024)")
        )

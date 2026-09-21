"""Migration 6: provider-neutral drive IDs and encrypted credentials."""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if "storage_policies" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("storage_policies")}
    if "credential_ciphertext" not in columns:
        connection.execute(text("ALTER TABLE storage_policies ADD COLUMN credential_ciphertext TEXT NULL"))
    if "credential_version" not in columns:
        connection.execute(text(
            "ALTER TABLE storage_policies ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 1"
        ))

    if connection.dialect.name == "mysql":
        connection.execute(text(
            "ALTER TABLE storage_policies MODIFY COLUMN provider VARCHAR(64) NOT NULL"
        ))
    elif connection.dialect.name == "postgresql":
        connection.execute(text(
            "ALTER TABLE storage_policies ALTER COLUMN provider TYPE VARCHAR(64) USING provider::text"
        ))

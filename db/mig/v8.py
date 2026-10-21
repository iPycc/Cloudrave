"""Migration 8: direct-upload checksums and writable operator drive mounts."""

from __future__ import annotations

import json

from sqlalchemy import Connection, MetaData, Table, inspect, select, text, update


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())

    if "upload_sessions" in tables:
        columns = {column["name"] for column in inspector.get_columns("upload_sessions")}
        if "provider_checksum" not in columns:
            connection.execute(text(
                "ALTER TABLE upload_sessions ADD COLUMN provider_checksum VARCHAR(128) NULL"
            ))
        if connection.dialect.name == "mysql":
            connection.execute(text(
                "ALTER TABLE upload_sessions MODIFY COLUMN upload_id TEXT NULL"
            ))
        elif connection.dialect.name == "postgresql":
            connection.execute(text(
                "ALTER TABLE upload_sessions ALTER COLUMN upload_id TYPE TEXT"
            ))

    if "bucket_mounts" in tables and "storage_policies" in tables:
        metadata = MetaData()
        mounts = Table("bucket_mounts", metadata, autoload_with=connection)
        policies = Table("storage_policies", metadata, autoload_with=connection)
        rows = list(connection.execute(
            select(mounts.c.id, mounts.c.extra)
            .join(policies, policies.c.id == mounts.c.policy_id)
            .where(policies.c.provider.in_(("drive_139", "drive_189", "drive_wo")))
        ).mappings())
        for row in rows:
            raw_extra = row["extra"]
            if isinstance(raw_extra, str):
                try:
                    raw_extra = json.loads(raw_extra)
                except json.JSONDecodeError:
                    raw_extra = {}
            extra = dict(raw_extra or {})
            extra["read_only"] = False
            connection.execute(
                update(mounts).where(mounts.c.id == row["id"]).values(extra=extra)
            )

"""Migration 7: normalize legacy enum member names to stable drive IDs."""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if "storage_policies" not in inspector.get_table_names():
        return

    connection.execute(text("""
        UPDATE storage_policies
        SET provider = CASE provider
            WHEN 'LOCAL_FS' THEN 'local_fs'
            WHEN 'TENCENT_COS' THEN 'tencent_cos'
            WHEN 'ALIYUN_OSS' THEN 'aliyun_oss'
            ELSE provider
        END
        WHERE provider IN ('LOCAL_FS', 'TENCENT_COS', 'ALIYUN_OSS')
    """))

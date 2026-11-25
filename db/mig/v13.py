"""Migration 13: version physical-object lifecycle transitions."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if "blob_refs" not in inspector.get_table_names():
        return
    if "revision" not in {column["name"] for column in inspector.get_columns("blob_refs")}:
        connection.execute(text("ALTER TABLE blob_refs ADD COLUMN revision BIGINT NOT NULL DEFAULT 0"))

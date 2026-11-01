"""Migration 4: allow large audit targets across strict SQL backends."""

from __future__ import annotations

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if "audit_logs" not in inspector.get_table_names():
        return
    if not any(column["name"] == "target" for column in inspector.get_columns("audit_logs")):
        return

    dialect = connection.dialect.name
    if dialect == "mysql":
        connection.execute(text("ALTER TABLE audit_logs MODIFY COLUMN target LONGTEXT NOT NULL"))
    elif dialect == "postgresql":
        connection.execute(text("ALTER TABLE audit_logs ALTER COLUMN target TYPE TEXT"))
    # SQLite already stores VARCHAR and TEXT with the same TEXT affinity and
    # never enforced the previous declared length, so no table rebuild is needed.

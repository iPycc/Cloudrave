"""Migration 5: retain six fractional seconds in MySQL datetime columns."""

from __future__ import annotations

from sqlalchemy import Connection, DateTime, inspect, text

from db.base import Base


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "mysql":
        return
    import models  # noqa: F401

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    quote = connection.dialect.identifier_preparer.quote
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        reflected = {column["name"]: column for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if not isinstance(column.type, DateTime):
                continue
            current = reflected.get(column.name)
            if current is None or getattr(current["type"], "fsp", None) == 6:
                continue
            nullability = "NULL" if current["nullable"] else "NOT NULL"
            connection.execute(text(
                f"ALTER TABLE {quote(table.name)} MODIFY COLUMN {quote(column.name)} "
                f"DATETIME(6) {nullability}"
            ))

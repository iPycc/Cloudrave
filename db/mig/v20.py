"""Migration 20: keep the object identity for guarded cleanup."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    if not any(column["name"] == "source_identity" for column in inspect(connection).get_columns("gc_tasks")):
        connection.execute(text("ALTER TABLE gc_tasks ADD COLUMN source_identity JSON NULL"))

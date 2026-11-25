"""Migration 19: distinguish queued recycle restores from active-tree operations."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    if not any(column["name"] == "is_restore" for column in inspect(connection).get_columns("path_plan_tasks")):
        connection.execute(text("ALTER TABLE path_plan_tasks ADD COLUMN is_restore BOOLEAN NOT NULL DEFAULT FALSE"))

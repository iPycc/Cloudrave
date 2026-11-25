"""Migration 15: record the destination name for queued renames."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    columns = inspect(connection).get_columns("path_plan_tasks")
    if not any(column["name"] == "rename_to" for column in columns):
        connection.execute(text("ALTER TABLE path_plan_tasks ADD COLUMN rename_to VARCHAR(255) NULL"))

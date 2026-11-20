"""Migration 16: record the destination folder for queued file moves."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    columns = inspect(connection).get_columns("path_plan_tasks")
    if not any(column["name"] == "move_to" for column in columns):
        connection.execute(text("ALTER TABLE path_plan_tasks ADD COLUMN move_to INTEGER NULL"))

"""Migration 17: record the target folder and name for file copies."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("path_plan_tasks")}
    if "copy_to" not in columns:
        connection.execute(text("ALTER TABLE path_plan_tasks ADD COLUMN copy_to INTEGER NULL"))
    if "copy_name" not in columns:
        connection.execute(text("ALTER TABLE path_plan_tasks ADD COLUMN copy_name VARCHAR(255) NULL"))

"""Migration 18: persist directory path operations and prepared objects."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if not any(column["name"] == "root_node_id" for column in inspector.get_columns("path_plan_tasks")):
        connection.execute(text("ALTER TABLE path_plan_tasks ADD COLUMN root_node_id INTEGER NULL"))
    if not any(column["name"] == "prepared" for column in inspector.get_columns("path_migration_jobs")):
        connection.execute(text("ALTER TABLE path_migration_jobs ADD COLUMN prepared INTEGER NOT NULL DEFAULT 0"))

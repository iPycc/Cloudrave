"""Migration 12: retain remote identity for path-migration prechecks."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    inspector = inspect(connection)
    if "path_plan_entries" not in inspector.get_table_names():
        return
    if "source_identity_json" not in {column["name"] for column in inspector.get_columns("path_plan_entries")}:
        connection.execute(text("ALTER TABLE path_plan_entries ADD COLUMN source_identity_json TEXT NULL"))

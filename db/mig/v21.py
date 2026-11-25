"""Persist the ownership token for uploads to named object paths."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    if not any(column["name"] == "path_token" for column in inspect(connection).get_columns("upload_sessions")):
        connection.execute(text("ALTER TABLE upload_sessions ADD COLUMN path_token VARCHAR(32) NULL"))

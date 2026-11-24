"""Keep the complete upload request identity with its retry key."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("upload_sessions")}
    if "request_hash" not in columns:
        connection.execute(text("ALTER TABLE upload_sessions ADD COLUMN request_hash VARCHAR(64) NULL"))

"""Persist upload session expiry and failures."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("upload_sessions")}
    for name, type_ in (("expires_at", "DATETIME"), ("last_seen_at", "DATETIME"),
                        ("error_reason", "VARCHAR(500)")):
        if name not in columns:
            connection.execute(text(f"ALTER TABLE upload_sessions ADD COLUMN {name} {type_} NULL"))
    connection.execute(text("UPDATE upload_sessions SET last_seen_at = updated_at WHERE last_seen_at IS NULL"))

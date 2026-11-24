"""Persist upload idempotency keys."""

from sqlalchemy import Connection, inspect, text


def upgrade(connection: Connection) -> None:
    columns = {column["name"] for column in inspect(connection).get_columns("upload_sessions")}
    if "idempotency_key" not in columns:
        connection.execute(text("ALTER TABLE upload_sessions ADD COLUMN idempotency_key VARCHAR(128) NULL"))
    indexes = {index["name"] for index in inspect(connection).get_indexes("upload_sessions")}
    if "ix_upload_sessions_owner_idempotency" not in indexes:
        connection.execute(text(
            "CREATE UNIQUE INDEX ix_upload_sessions_owner_idempotency "
            "ON upload_sessions (owner_id, idempotency_key)"
        ))

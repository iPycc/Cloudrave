"""Store successful responses for retryable file operations."""

from sqlalchemy import Connection

from models.tables import IdempotencyRecord


def upgrade(connection: Connection) -> None:
    IdempotencyRecord.__table__.create(connection, checkfirst=True)

"""Migration 9: durable, paginated import conflict reports."""

from sqlalchemy import Connection

from models.tables import MountSyncConflict


def upgrade(connection: Connection) -> None:
    MountSyncConflict.__table__.create(connection, checkfirst=True)

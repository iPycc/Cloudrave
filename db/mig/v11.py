"""Migration 11: durable storage-path migration prechecks."""

from sqlalchemy import Connection

from models.tables import PathPlanEntry, PathPlanTask


def upgrade(connection: Connection) -> None:
    PathPlanTask.__table__.create(connection, checkfirst=True)
    PathPlanEntry.__table__.create(connection, checkfirst=True)

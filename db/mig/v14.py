"""Migration 14: durable, per-object path-migration journal."""

from sqlalchemy import Connection

from models.tables import PathMigrationJob, PathMigrationRecord


def upgrade(connection: Connection) -> None:
    PathMigrationJob.__table__.create(connection, checkfirst=True)
    PathMigrationRecord.__table__.create(connection, checkfirst=True)

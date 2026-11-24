"""Queue file hashes and thumbnails after upload completion."""

from sqlalchemy import Connection

from models.tables import DerivedTask


def upgrade(connection: Connection) -> None:
    DerivedTask.__table__.create(connection, checkfirst=True)

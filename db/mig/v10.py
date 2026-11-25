"""Migration 10: coordinate import-worker admission across processes."""

from sqlalchemy import Connection, select

from models.tables import SYNC_CLAIM_GATE_KEY, SystemMeta


def upgrade(connection: Connection) -> None:
    table = SystemMeta.__table__
    if connection.scalar(select(table.c.key).where(table.c.key == SYNC_CLAIM_GATE_KEY)) is None:
        connection.execute(table.insert().values(key=SYNC_CLAIM_GATE_KEY, value="1"))

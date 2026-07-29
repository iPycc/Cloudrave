"""Migration registry and runner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, select

from db.mig.row import SchemaMigration
from db.mig.v1 import upgrade as upgrade_v1
from db.mig.v2 import upgrade as upgrade_v2


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[Connection], None]


MIGRATIONS = (
    Migration(1, "legacy-runtime-schema", upgrade_v1),
    Migration(2, "user-thumbnail-preference", upgrade_v2),
)


def run_migrations(connection: Connection) -> None:
    versions = [migration.version for migration in MIGRATIONS]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError("Database migrations must be contiguous and start at version 1")

    applied = set(
        connection.scalars(select(SchemaMigration.version)).all()
    )
    unknown = applied.difference(versions)
    if unknown:
        raise RuntimeError(
            f"Database schema is newer than this Cloudrave build: {sorted(unknown)}"
        )

    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        migration.upgrade(connection)
        connection.execute(
            SchemaMigration.__table__.insert().values(
                version=migration.version,
                name=migration.name,
                applied_at=datetime.now(UTC),
            )
        )

"""Opt-in destructive integration coverage for all cross-backend directions.

Set CLOUDRAVE_RUN_DATABASE_INTEGRATION=1 plus dedicated (disposable)
CLOUDRAVE_TEST_MYSQL_URL and CLOUDRAVE_TEST_POSTGRESQL_URL databases.
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, select

from core.cfg import CloudraveConfig, DatabaseBackend, load_config
from db.base import create_schema, make_async_engine
from models.enums import NodeType, StoragePolicyType, UserRole, UserStatus
from models.tables import AuditLog, BucketMount, Node, StoragePolicy, User
from services.admin.database import DatabaseMigrationManager, MigrationJob, _application_tables


MYSQL_URL = os.getenv("CLOUDRAVE_TEST_MYSQL_URL", "")
POSTGRESQL_URL = os.getenv("CLOUDRAVE_TEST_POSTGRESQL_URL", "")
pytestmark = pytest.mark.skipif(
    os.getenv("CLOUDRAVE_RUN_DATABASE_INTEGRATION") != "1" or not MYSQL_URL or not POSTGRESQL_URL,
    reason="dedicated MySQL/PostgreSQL integration databases were not configured",
)


def _profile(base: CloudraveConfig, backend: DatabaseBackend, sqlite_path: Path) -> CloudraveConfig:
    urls = {
        "sqlite": f"sqlite+aiosqlite:///{sqlite_path.as_posix()}",
        "mysql": MYSQL_URL,
        "postgresql": POSTGRESQL_URL,
    }
    return replace(base, database=replace(base.database, backend=backend, url=urls[backend]))


async def _clear(engine) -> None:
    async with engine.begin() as connection:
        for table in reversed(_application_tables()):
            await connection.execute(delete(table))


async def _seed(engine) -> None:
    async with engine.begin() as connection:
        await connection.execute(insert(User).values(
            id=41,
            uid="ADMIN041",
            email="integration-admin@example.test",
            username="迁移管理员",
            password_hash="hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            timezone="Asia/Shanghai",
            thumbnails_enabled=True,
            totp_enabled=False,
            totp_backup_codes=["恢复码"],
        ))
        await connection.execute(insert(StoragePolicy).values(
            id=7,
            name="integration-local",
            provider=StoragePolicyType.LOCAL_FS,
            bucket_name="integration",
            extra={"unicode": "数据"},
        ))
        await connection.execute(insert(BucketMount).values(
            id=9,
            owner_id=41,
            policy_id=7,
            name="迁移目录",
            mount_slug="integration",
            quota_bytes=2**50,
            extra={"mode": "managed"},
        ))
        await connection.execute(insert(Node), [
            {"id": 101, "owner_id": 41, "mount_id": 9, "parent_id": None, "type": NodeType.FOLDER, "name": "父目录", "size": 0},
            {"id": 102, "owner_id": 41, "mount_id": 9, "parent_id": 101, "type": NodeType.FILE, "name": "子文件.txt", "size": 2**40},
        ])
        await connection.execute(insert(AuditLog).values(
            id=301,
            actor_id=41,
            action="batch-delete",
            target="nodes:" + ",".join(str(index) for index in range(4000)),
        ))


@pytest.mark.parametrize(
    ("source_backend", "target_backend"),
    [
        ("sqlite", "mysql"),
        ("sqlite", "postgresql"),
        ("mysql", "sqlite"),
        ("mysql", "postgresql"),
        ("postgresql", "sqlite"),
        ("postgresql", "mysql"),
    ],
)
@pytest.mark.asyncio
async def test_all_cross_backend_directions(
    tmp_path: Path,
    source_backend: DatabaseBackend,
    target_backend: DatabaseBackend,
) -> None:
    base = load_config(tmp_path)
    source_config = _profile(base, source_backend, tmp_path / f"source-{target_backend}.db")
    target_config = _profile(base, target_backend, tmp_path / f"target-{source_backend}.db")
    source = make_async_engine(source_config)
    target = make_async_engine(target_config)
    try:
        await create_schema(source)
        await create_schema(target)
        await _clear(source)
        await _clear(target)
        await _seed(source)

        manager = DatabaseMigrationManager(tmp_path)
        job = MigrationJob(
            id="22222222-2222-2222-2222-222222222222",
            source_backend=source_backend,
            target_backend=target_backend,
            data_choice="source",
            total_rows=6,
        )
        await manager._copy_database(source, target, job)

        async with target.begin() as connection:
            child = (await connection.execute(
                select(Node.__table__.c.parent_id, Node.__table__.c.name, Node.__table__.c.size)
                .where(Node.__table__.c.id == 102)
            )).one()
            result = await connection.execute(insert(User).values(
                uid="USER042",
                email="integration-user@example.test",
                username="新增用户",
                password_hash="hash",
                role=UserRole.USER,
                status=UserStatus.ACTIVE,
                timezone="UTC",
                thumbnails_enabled=True,
                totp_enabled=False,
                totp_backup_codes=[],
            ))
            new_id = result.inserted_primary_key[0]
        assert child == (101, "子文件.txt", 2**40)
        assert int(new_id) > 41
        async with target.connect() as connection:
            audit_target = await connection.scalar(
                select(AuditLog.__table__.c.target).where(AuditLog.__table__.c.id == 301)
            )
        assert audit_target is not None and len(audit_target) > 255
    finally:
        await source.dispose()
        await target.dispose()

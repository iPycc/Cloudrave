from __future__ import annotations

from configparser import ConfigParser
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

from asyncpg.exceptions import ConnectionDoesNotExistError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable

from core.cfg import database_config_for_backend, load_config
from db.base import Base, create_schema, make_async_engine
from db.switch_state import load_switch_state, save_switch_state
from models.enums import UserRole, UserStatus
from models.tables import AuditLog, Node, User
from schemas.database import DatabaseConfigUpdate, DatabaseConnectionRead
import services.admin.database as database_service
from services.admin.database import (
    DatabaseMigrationManager,
    MigrationJob,
    _config_with_overrides,
    _connection_error_code,
    _connection_error_message,
    _digest_value,
    _migration_error_message,
    _parent_first_rows,
    _repair_source_foreign_keys,
    _RowDigest,
    _sanitize_message,
)
from web.mid.database import DatabaseMaintenanceMiddleware


def _config(root: Path):
    config = load_config(root)
    return config


def test_database_backend_is_strictly_validated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    text = config.config_path.read_text(encoding="utf-8")
    config.config_path.write_text(text.replace("database = sqlite", "database = oracle"), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported.*oracle"):
        load_config(tmp_path)


def test_missing_state_infers_existing_sqlite_as_source(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sqlite_path = tmp_path / "data" / "cloudrave.db"
    sqlite_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(sqlite_path)
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO users (id) VALUES (1)")
    connection.commit()
    connection.close()

    text = config.config_path.read_text(encoding="utf-8")
    config.config_path.write_text(text.replace("database = sqlite", "database = mysql"), encoding="utf-8")
    requested = load_config(tmp_path)
    state = load_switch_state(requested)

    assert state.active_backend == "sqlite"
    assert state.requested_backend == "mysql"
    assert state.status == "pending"


@pytest.mark.asyncio
async def test_historical_migration_is_not_the_current_pending_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.config_path.write_text(
        config.config_path.read_text(encoding="utf-8").replace(
            "database = sqlite", "database = postgresql"
        ),
        encoding="utf-8",
    )
    manager = DatabaseMigrationManager(tmp_path)
    manager.state.active_backend = "mysql"
    manager.state.status = "pending"
    manager.state.requested_backend = "postgresql"
    old_job = MigrationJob(
        id="77777777-7777-7777-7777-777777777777",
        source_backend="sqlite",
        target_backend="mysql",
        data_choice="source",
        phase="ready",
        progress=100,
        restart_required=False,
    )
    manager._current_job = old_job
    manager._persist_job(old_job)
    manager.state.migration_id = old_job.id
    save_switch_state(tmp_path, manager.state)

    async def inspect_engine(self, _engine, backend, **_kwargs):
        return DatabaseConnectionRead(reachable=True, backend=backend)

    async def inspect_config(self, config, **_kwargs):
        return DatabaseConnectionRead(reachable=True, backend=config.database.backend)

    monkeypatch.setattr(DatabaseMigrationManager, "inspect_engine", inspect_engine)
    monkeypatch.setattr(DatabaseMigrationManager, "inspect_config", inspect_config)
    dep = type("FakeDep", (), {
        "config": database_config_for_backend(load_config(tmp_path), "mysql"),
        "engine": object(),
    })()

    status = await manager.status(dep)

    assert status.switch_pending is True
    assert status.current_migration is None
    assert status.recent_migrations[0].id == old_job.id
    assert manager.state.migration_id is None


@pytest.mark.asyncio
async def test_config_update_preserves_unknown_keys_and_password(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with config.config_path.open("a", encoding="utf-8") as output:
        output.write("\n[custom]\nkeep_me = yes\n")
    manager = DatabaseMigrationManager(tmp_path)

    profile = await manager.update_config(DatabaseConfigUpdate(
        backend="mysql",
        host="db.internal",
        port=3307,
        user="cloudrave",
        password="super-secret",
        name="cloudrave_prod",
    ))
    await manager.update_config(DatabaseConfigUpdate(
        backend="mysql",
        host="db.internal",
        port=3307,
        user="cloudrave",
        password="",
        name="cloudrave_prod",
    ))

    parser = ConfigParser()
    parser.read(config.config_path, encoding="utf-8")
    assert parser.get("custom", "keep_me") == "yes"
    assert parser.get("database", "mysql_password") == "super-secret"
    assert profile.password_configured is True
    assert "super-secret" not in profile.model_dump_json()
    assert config.config_path.with_suffix(".ini.bak").is_file()


@pytest.mark.asyncio
async def test_maintenance_gate_blocks_writes_but_keeps_reads() -> None:
    app = FastAPI()
    app.state.database_manager = type("Manager", (), {"maintenance": True})()
    app.add_middleware(DatabaseMaintenanceMiddleware)

    @app.get("/read")
    async def read():
        return {"ok": True}

    @app.post("/write")
    async def write():
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/read")).status_code == 200
        response = await client.post("/write")
    assert response.status_code == 503
    assert response.json()["error"] == "database_migration_in_progress"


@pytest.mark.asyncio
async def test_sqlite_copy_verifies_rows_and_writes_instance_marker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = make_async_engine(config)
    target_config = load_config(tmp_path)
    target_config.database.url = f"sqlite+aiosqlite:///{(tmp_path / 'target.db').as_posix()}"
    target = make_async_engine(target_config)
    await create_schema(source)
    await create_schema(target)

    async with source.begin() as connection:
        await connection.execute(insert(User).values(
            uid="ADMIN001",
            email="admin@example.test",
            username="管理员",
            password_hash="hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            timezone="Asia/Shanghai",
            thumbnails_enabled=True,
            totp_enabled=False,
            totp_backup_codes=["恢复码"],
        ))

    manager = DatabaseMigrationManager(tmp_path)
    save_switch_state(tmp_path, manager.state)
    job = MigrationJob(
        id="11111111-1111-1111-1111-111111111111",
        source_backend="sqlite",
        target_backend="sqlite",
        data_choice="source",
        total_rows=1,
    )
    await manager._copy_database(source, target, job)

    async with target.connect() as connection:
        username = await connection.scalar(select(User.__table__.c.username))
        marker = await connection.scalar(
            select(Base.metadata.tables["system_meta"].c.value).where(
                Base.metadata.tables["system_meta"].c.key == "database.migration_id"
            )
        )
    assert username == "管理员"
    assert marker == job.id

    backup_path = await manager._backup_target(target, job)
    with gzip.open(backup_path, "rt", encoding="utf-8") as backup:
        lines = backup.read().splitlines()
    header, footer = json.loads(lines[0]), json.loads(lines[-1])
    content = "".join(f"{line}\n" for line in lines[1:-1]).encode("utf-8")
    assert header["type"] == "cloudrave-database-backup"
    assert footer["algorithm"] == "sha256"
    assert footer["content_sha256"] == hashlib.sha256(content).hexdigest()

    await source.dispose()
    await target.dispose()


def test_connection_errors_are_redacted() -> None:
    message = "mysql+aiomysql://root:secret@db/cloudrave?password=another"
    sanitized = _sanitize_message(message)
    assert "secret" not in sanitized
    assert "another" not in sanitized
    assert sanitized.count("***") == 2


def test_missing_database_error_is_translated_for_the_wizard() -> None:
    class DriverFailure(Exception):
        pass

    wrapped = DriverFailure("connection failed")
    wrapped.orig = DriverFailure(1049, "Unknown database 'cloudrave'")

    assert _connection_error_code(wrapped) == "database_not_found"
    message = _connection_error_message(wrapped, "mysql", "cloudrave")
    assert message == "目标数据库“cloudrave”不存在"
    assert "OperationalError" not in message
    assert "sqlalchemy.me" not in message


@pytest.mark.asyncio
async def test_postgresql_driver_disconnect_is_returned_as_a_connection_result(tmp_path: Path) -> None:
    class DisconnectedEngine:
        def connect(self):
            raise ConnectionDoesNotExistError("connection was closed")

    result = await DatabaseMigrationManager(tmp_path).inspect_engine(
        DisconnectedEngine(), "postgresql", database_name="cloudrave",
    )
    assert result.reachable is False
    assert result.error_code == "connection_failed"
    assert "PostgreSQL" in result.error


def test_audit_target_uses_mysql_longtext_and_errors_are_compact() -> None:
    ddl = str(CreateTable(AuditLog.__table__).compile(dialect=mysql.dialect()))
    assert "target LONGTEXT NOT NULL" in ddl

    class DriverFailure(Exception):
        pass

    wrapped = DriverFailure("migration failed")
    wrapped.orig = DriverFailure(1406, "Data too long for column 'target' at row 43")
    message = _migration_error_message(wrapped, "audit_logs")
    assert message == "目标数据库字段长度不足（表：audit_logs），目标事务已回滚；请升级数据库结构后重试"
    assert "INSERT INTO" not in message


def test_self_referential_rows_are_inserted_parent_first() -> None:
    rows = [
        {"id": 1, "parent_id": 2},
        {"id": 2, "parent_id": None},
        {"id": 3, "parent_id": 1},
    ]

    ordered = _parent_first_rows(Node.__table__, rows)

    assert [row["id"] for row in ordered] == [2, 1, 3]


def test_copy_digest_is_independent_of_backend_primary_key_collation() -> None:
    source = _RowDigest()
    target = _RowDigest()
    rows = [{"id": "A", "value": 1}, {"id": "a", "value": 2}]
    for row in rows:
        source.update(row)
    for row in reversed(rows):
        target.update(row)
    assert source.hexdigest() == target.hexdigest()

    changed = _RowDigest()
    changed.update(rows[0])
    changed.update({"id": "a", "value": 3})
    assert changed.hexdigest() != source.hexdigest()


def test_source_orphans_are_repaired_only_in_target_and_logged() -> None:
    lookup = {Node.__table__.c.id: {1, 2}}
    repairs = {}
    upload_table = Base.metadata.tables["upload_sessions"]
    upload = _repair_source_foreign_keys(
        upload_table,
        {"node_id": 99, "owner_id": None, "mount_id": None},
        lookup,
        repairs,
    )
    assert upload is not None and upload["node_id"] is None
    assert repairs[("upload_sessions", "node_id", "null")] == 1

    share_item_table = Base.metadata.tables["share_items"]
    orphan = _repair_source_foreign_keys(
        share_item_table,
        {"node_id": 99, "share_id": None},
        lookup,
        repairs,
    )
    assert orphan is None
    assert repairs[("share_items", "node_id", "skip")] == 1


def test_mysql_json_float_round_trip_has_a_stable_digest() -> None:
    source = {"payload": [{"y": 0.11817102137767221}]}
    mysql_readback = {"payload": [{"y": 0.1181710213776722}]}
    assert _digest_value(source) == _digest_value(mysql_readback)


@pytest.mark.asyncio
async def test_connection_test_creates_a_missing_remote_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_with_overrides(
        _config(tmp_path),
        DatabaseConfigUpdate(
            backend="mysql",
            host="127.0.0.1",
            port=3306,
            user="root",
            password="secret",
            name="cloudrave",
        ),
    )

    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engines = [FakeEngine(), FakeEngine()]
    monkeypatch.setattr(database_service, "make_async_engine", lambda _config: engines.pop(0))
    probes = 0

    async def inspect_engine(self, engine, backend, **_kwargs):
        nonlocal probes
        probes += 1
        if probes == 1:
            return DatabaseConnectionRead(
                reachable=False,
                backend=backend,
                error_code="database_not_found",
                error="目标数据库“cloudrave”不存在",
            )
        return DatabaseConnectionRead(reachable=True, backend=backend)

    created: list[str] = []

    async def create_remote_database(target_config):
        created.append(target_config.database.backend)
        return True

    monkeypatch.setattr(DatabaseMigrationManager, "inspect_engine", inspect_engine)
    monkeypatch.setattr(database_service, "_create_remote_database", create_remote_database)

    result = await DatabaseMigrationManager(tmp_path).inspect_config(
        config,
        verify_permissions=True,
        create_if_missing=True,
    )

    assert result.reachable is True
    assert result.database_created is True
    assert result.error is None
    assert created == ["mysql"]
    assert probes == 2


@pytest.mark.asyncio
async def test_postgresql_closed_connection_probes_catalog_and_creates_missing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_with_overrides(
        _config(tmp_path),
        DatabaseConfigUpdate(
            backend="postgresql", host="127.0.0.1", port=5432,
            user="postgres", password="secret", name="cloudrave",
        ),
    )

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    attempts = 0

    async def inspect_engine(self, _engine, backend, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return DatabaseConnectionRead(
                reachable=False, backend=backend,
                error_code="connection_failed", error="无法连接 PostgreSQL 服务",
            )
        return DatabaseConnectionRead(reachable=True, backend=backend)

    catalog_probes = 0

    async def database_exists(_config):
        nonlocal catalog_probes
        catalog_probes += 1
        return False

    created: list[str] = []

    async def create_database(target_config):
        created.append(target_config.database.backend)
        return True

    monkeypatch.setattr(database_service, "make_async_engine", lambda _config: FakeEngine())
    monkeypatch.setattr(DatabaseMigrationManager, "inspect_engine", inspect_engine)
    monkeypatch.setattr(database_service, "_postgresql_database_exists", database_exists)
    monkeypatch.setattr(database_service, "_create_remote_database", create_database)
    manager = DatabaseMigrationManager(tmp_path)

    status_result = await manager.inspect_config(config)
    assert status_result.error_code == "database_not_found"
    assert "cloudrave" in status_result.error
    assert created == []

    test_result = await manager.inspect_config(config, create_if_missing=True)
    assert test_result.reachable is True
    assert test_result.database_created is True
    assert created == ["postgresql"]
    assert catalog_probes == 2


@pytest.mark.asyncio
async def test_postgresql_closed_connection_does_not_recreate_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_with_overrides(
        _config(tmp_path),
        DatabaseConfigUpdate(
            backend="postgresql", host="127.0.0.1", port=5432,
            user="postgres", password="secret", name="cloudrave",
        ),
    )

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    async def inspect_engine(self, _engine, backend, **_kwargs):
        return DatabaseConnectionRead(
            reachable=False, backend=backend,
            error_code="connection_failed", error="无法连接 PostgreSQL 服务",
        )

    async def unexpected_create(_config):
        raise AssertionError("an existing PostgreSQL database must not be recreated")

    monkeypatch.setattr(database_service, "make_async_engine", lambda _config: FakeEngine())
    monkeypatch.setattr(DatabaseMigrationManager, "inspect_engine", inspect_engine)
    monkeypatch.setattr(database_service, "_postgresql_database_exists", lambda _config: _true())
    monkeypatch.setattr(database_service, "_create_remote_database", unexpected_create)

    async def _true():
        return True

    result = await DatabaseMigrationManager(tmp_path).inspect_config(
        config, create_if_missing=True,
    )
    assert result.reachable is False
    assert result.database_created is False


@pytest.mark.asyncio
async def test_connection_test_reports_existing_database_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_with_overrides(
        _config(tmp_path),
        DatabaseConfigUpdate(
            backend="mysql", host="127.0.0.1", port=3306,
            user="root", password="secret", name="cloudrave",
        ),
    )

    class FakeEngine:
        async def dispose(self) -> None:
            return None

    async def inspect_engine(self, _engine, backend, **_kwargs):
        return DatabaseConnectionRead(reachable=True, backend=backend)

    async def unexpected_create(_config):
        raise AssertionError("an existing database must not be recreated")

    monkeypatch.setattr(database_service, "make_async_engine", lambda _config: FakeEngine())
    monkeypatch.setattr(DatabaseMigrationManager, "inspect_engine", inspect_engine)
    monkeypatch.setattr(database_service, "_create_remote_database", unexpected_create)

    result = await DatabaseMigrationManager(tmp_path).inspect_config(
        config, verify_permissions=True, create_if_missing=True,
    )
    assert result.reachable is True
    assert result.database_created is False
    assert result.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "port", "expected_database"),
    [("mysql", 3306, None), ("postgresql", 5432, "postgres")],
)
async def test_remote_creation_uses_a_server_level_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    port: int,
    expected_database: str | None,
) -> None:
    config = _config_with_overrides(
        _config(tmp_path),
        DatabaseConfigUpdate(
            backend=backend,
            host="127.0.0.1",
            port=port,
            user="root",
            password="secret",
            name="cloudrave",
        ),
    )
    captured = []

    class StopBeforeConnect(Exception):
        pass

    class FakeEngine:
        def connect(self):
            raise StopBeforeConnect

        async def dispose(self) -> None:
            return None

    def fake_engine(maintenance_config):
        captured.append(maintenance_config)
        return FakeEngine()

    monkeypatch.setattr(database_service, "make_async_engine", fake_engine)

    with pytest.raises(StopBeforeConnect):
        await database_service._create_remote_database(config)

    assert make_url(captured[0].database.url).database == expected_database


@pytest.mark.asyncio
async def test_copy_verification_failure_rolls_back_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source = make_async_engine(config)
    target_config = load_config(tmp_path)
    target_config.database.url = f"sqlite+aiosqlite:///{(tmp_path / 'rollback-target.db').as_posix()}"
    target = make_async_engine(target_config)
    await create_schema(source)
    await create_schema(target)
    base_user = {
        "password_hash": "hash",
        "role": UserRole.ADMIN,
        "status": UserStatus.ACTIVE,
        "timezone": "UTC",
        "thumbnails_enabled": True,
        "totp_enabled": False,
        "totp_backup_codes": [],
    }
    async with source.begin() as connection:
        await connection.execute(insert(User).values(
            id=1, uid="SOURCE01", email="source@example.test", username="source", **base_user
        ))
    async with target.begin() as connection:
        await connection.execute(insert(User).values(
            id=2, uid="TARGET02", email="target@example.test", username="target", **base_user
        ))

    calls = 0

    def mismatched_digest(_row):
        nonlocal calls
        calls += 1
        return b"source\n" if calls == 1 else b"target\n"

    monkeypatch.setattr(database_service, "_canonical_row", mismatched_digest)
    manager = DatabaseMigrationManager(tmp_path)
    job = MigrationJob(
        id="33333333-3333-3333-3333-333333333333",
        source_backend="sqlite",
        target_backend="sqlite",
        data_choice="source",
        total_rows=1,
    )
    with pytest.raises(RuntimeError, match="校验不一致"):
        await manager._copy_database(source, target, job)

    async with target.connect() as connection:
        usernames = (await connection.execute(select(User.__table__.c.username))).scalars().all()
    assert usernames == ["target"]

    await source.dispose()
    await target.dispose()


@pytest.mark.asyncio
async def test_interrupted_job_recovers_committed_target_marker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = make_async_engine(config)
    await create_schema(engine)
    migration_id = "44444444-4444-4444-4444-444444444444"
    meta = Base.metadata.tables["system_meta"]
    async with engine.begin() as connection:
        await connection.execute(insert(meta).values(
            key="database.migration_id",
            value=migration_id,
        ))
    await engine.dispose()

    manager = DatabaseMigrationManager(tmp_path)
    manager.state.status = "migrating"
    manager.state.requested_backend = "sqlite"
    manager.state.migration_id = migration_id
    save_switch_state(tmp_path, manager.state)
    job = MigrationJob(
        id=migration_id,
        source_backend="mysql",
        target_backend="sqlite",
        data_choice="source",
        phase="verifying",
    )
    manager._persist_job(job)

    recovered = DatabaseMigrationManager(tmp_path)
    assert recovered._interrupted_job is not None
    await recovered._recover_interrupted_job_state(recovered._interrupted_job)
    assert recovered.state.status == "activation_pending"
    assert recovered._current_job is not None
    assert recovered._current_job.phase == "ready"

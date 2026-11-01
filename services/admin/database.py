"""Database configuration, inspection, and cross-backend migration service."""

from __future__ import annotations

import asyncio
import base64
from configparser import ConfigParser
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Literal, TYPE_CHECKING
from uuid import uuid4

from asyncpg import PostgresError
from fastapi import HTTPException, status
from sqlalchemy import DateTime, delete, func, insert, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.cfg import (
    CloudraveConfig,
    DatabaseBackend,
    database_config_for_backend,
    database_management_source,
    load_config,
)
from db.base import Base, create_schema, make_async_engine
from db.switch_state import (
    DatabaseSwitchState,
    database_fingerprint,
    load_switch_state,
    save_switch_state,
)
from schemas.database import (
    DatabaseConfigUpdate,
    DatabaseConnectionRead,
    DatabaseDataSummary,
    DatabaseMigrationLogRead,
    DatabaseMigrationRead,
    DatabaseProfileRead,
    DatabaseStatusRead,
    DatabaseTableSummary,
)

if TYPE_CHECKING:
    from core.dep import Dep
    from models.tables import User


MIGRATION_DIR = "database-migrations"
BACKUP_DIR = "database-backups"
INTERNAL_TABLES = {"schema_migrations"}
LOG_MESSAGE_LIMIT = 2000
COPY_BATCH_SIZE = 500


class _RowDigest:
    """Order-independent digest for a table's row multiset.

    Database collations can order string primary keys differently, even when
    every copied row is identical. A sum of cryptographic row hashes together
    with the row count avoids treating that ordering as a content difference.
    """

    def __init__(self) -> None:
        self.count = 0
        self.checksum = 0

    def update(self, row: dict[str, Any]) -> None:
        digest = hashlib.sha256(_canonical_row(row)).digest()
        self.checksum = (self.checksum + int.from_bytes(digest)) % (1 << 256)
        self.count += 1

    def hexdigest(self) -> str:
        return f"{self.count}:{self.checksum:064x}"


@dataclass(slots=True)
class MigrationJob:
    id: str
    source_backend: DatabaseBackend
    target_backend: DatabaseBackend
    data_choice: Literal["source", "target"]
    phase: str = "queued"
    progress: int = 0
    current_table: str | None = None
    processed_rows: int = 0
    total_rows: int = 0
    detail: str = "等待迁移任务启动"
    restart_required: bool = False
    backup_path: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    log_seq: int = 0


class DatabaseMigrationManager:
    def __init__(
        self,
        root_dir: Path,
        state: DatabaseSwitchState | None = None,
    ) -> None:
        self.root_dir = root_dir
        initial = load_config(root_dir)
        self.state = state or load_switch_state(initial)
        self.maintenance = False
        self._active_write_requests = 0
        self._writes_drained = asyncio.Event()
        self._writes_drained.set()
        self._job_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._current_job: MigrationJob | None = self._load_latest_job()
        self._interrupted_job: MigrationJob | None = None
        if self._current_job and self._current_job.phase in {
            "queued", "connecting", "inventory", "backup", "schema", "copying", "verifying"
        }:
            self._interrupted_job = self._current_job

    @property
    def active_backend(self) -> DatabaseBackend:
        return self.state.active_backend

    def begin_business_write(self) -> bool:
        if self.maintenance:
            return False
        self._active_write_requests += 1
        self._writes_drained.clear()
        return True

    def end_business_write(self) -> None:
        self._active_write_requests = max(0, self._active_write_requests - 1)
        if self._active_write_requests == 0:
            self._writes_drained.set()

    async def wait_for_business_writes(self) -> None:
        await self._writes_drained.wait()

    def requested_config(self) -> CloudraveConfig:
        return load_config(self.root_dir)

    def config_for(self, backend: DatabaseBackend) -> CloudraveConfig:
        return database_config_for_backend(self.requested_config(), backend)

    def managed_by(self) -> str:
        parser = ConfigParser()
        parser.read(self.root_dir / "config.ini", encoding="utf-8")
        return database_management_source(parser)

    def mark_runtime_ready(self, runtime_config: CloudraveConfig) -> None:
        runtime_backend = runtime_config.database.backend
        if self.state.status == "activation_pending" and self.state.requested_backend == runtime_backend:
            activated_migration_id = self.state.migration_id
            self.state.active_backend = runtime_backend
            self.state.active_fingerprint = database_fingerprint(runtime_config)
            self.state.status = "active"
            self.state.requested_backend = None
            self.state.last_error = None
            if (
                self._current_job is not None
                and self._current_job.id == activated_migration_id
                and self._current_job.phase == "ready"
            ):
                self._log(self._current_job, "info", f"{runtime_backend} 数据库已在重启后激活")
                self._set(
                    self._current_job,
                    detail="目标数据库已激活",
                    restart_required=False,
                )
        elif self.state.status == "active":
            self.state.active_backend = runtime_backend
            self.state.active_fingerprint = database_fingerprint(runtime_config)
        save_switch_state(self.root_dir, self.state)

    async def ensure_instance_marker(self, engine: AsyncEngine) -> None:
        table = Base.metadata.tables["system_meta"]
        async with engine.begin() as connection:
            await connection.execute(delete(table).where(table.c.key == "database.instance_id"))
            await connection.execute(insert(table).values(
                key="database.instance_id",
                value=self.state.instance_id,
                updated_at=datetime.now(UTC),
            ))

    def activation_fallback_config(self) -> CloudraveConfig | None:
        if self.state.status != "activation_pending":
            return None
        return self.config_for(self.state.active_backend)

    def mark_activation_failed(self, error: str) -> None:
        self.state.status = "activation_failed"
        self.state.last_error = _sanitize_message(error)
        save_switch_state(self.root_dir, self.state)

    async def status(self, dep: "Dep", *, after_seq: int = 0) -> DatabaseStatusRead:
        if self._interrupted_job is not None:
            await self._recover_interrupted_job_state(self._interrupted_job)
            self._interrupted_job = None
        requested_config = self.requested_config()
        managed_by = self.managed_by()
        requested_backend = requested_config.database.backend
        active_backend = dep.config.database.backend
        switch_pending = requested_backend != active_backend

        if switch_pending and (
            self.state.status == "active"
            or (self.state.status == "pending" and self.state.requested_backend != requested_backend)
        ):
            self.state.status = "pending"
            self.state.requested_backend = requested_backend
            self.state.migration_id = None
            save_switch_state(self.root_dir, self.state)

        current_job = self._current_job
        current_job_matches = bool(
            switch_pending
            and current_job
            and current_job.id == self.state.migration_id
            and current_job.source_backend == active_backend
            and current_job.target_backend == requested_backend
            and (current_job.phase != "ready" or current_job.restart_required)
        )
        if self.state.status == "pending" and self.state.migration_id and not current_job_matches:
            # A completed migration to another backend remains in history, not
            # in the current switch. Drop only the stale pointer, not the job.
            self.state.migration_id = None
            save_switch_state(self.root_dir, self.state)

        source = await self.inspect_engine(dep.engine, active_backend)
        target = None
        if switch_pending:
            target = await self.inspect_config(
                database_config_for_backend(requested_config, requested_backend)
            )

        current = self._migration_read(current_job, after_seq=after_seq) if current_job_matches and current_job else None
        recent = [
            self._migration_read(job, after_seq=job.log_seq)
            for job in self._load_recent_jobs(limit=5)
        ]
        warning = self.state.last_error
        if managed_by != "config":
            warning = (
                "CLOUDRAVE_DATABASE_URL 正在覆盖 config.ini。"
                if managed_by == "environment"
                else "[database].url 正在覆盖分字段数据库配置。"
            )

        return DatabaseStatusRead(
            managed_by=managed_by,  # type: ignore[arg-type]
            editable=managed_by == "config" and not self.maintenance,
            config_path=str(requested_config.config_path),
            active=_profile(dep.config),
            requested=_profile(requested_config),
            profiles=[
                _profile(database_config_for_backend(requested_config, backend))
                for backend in ("sqlite", "mysql", "postgresql")
            ],
            switch_pending=switch_pending,
            activation_pending=self.state.status == "activation_pending",
            activation_failed=self.state.status == "activation_failed",
            source=source,
            target=target,
            current_migration=current,
            recent_migrations=recent,
            warning=warning,
        )

    async def update_config(self, payload: DatabaseConfigUpdate) -> DatabaseProfileRead:
        if self.managed_by() != "config":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Database settings are controlled by an environment variable or explicit URL",
            )
        if self.state.status in {"migrating", "activation_pending"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="当前迁移尚未结束或仍在等待重启，不能更改目标配置",
            )
        if payload.backend == self.state.active_backend:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="首版仅支持不同数据库类型之间切换",
            )
        _validate_config_payload(payload)
        updates: dict[str, str] = {"database": payload.backend}
        if payload.backend == "sqlite":
            if payload.sqlite_path:
                updates["sqlite_path"] = payload.sqlite_path.strip()
        else:
            prefix = "mysql" if payload.backend == "mysql" else "postgresql"
            if payload.host is not None:
                updates[f"{prefix}_host"] = payload.host.strip()
            if payload.port is not None:
                updates[f"{prefix}_port"] = str(payload.port)
            if payload.user is not None:
                updates[f"{prefix}_user"] = payload.user.strip()
            if payload.password:
                updates[f"{prefix}_password"] = payload.password
            if payload.name is not None:
                updates[f"{prefix}_name"] = payload.name.strip()
        _update_ini_section_atomic(self.root_dir / "config.ini", "database", updates)
        configured = self.requested_config()
        if configured.database.backend != self.state.active_backend:
            self.state.status = "pending"
            self.state.requested_backend = configured.database.backend
            self.state.migration_id = None
            self.state.last_error = None
            save_switch_state(self.root_dir, self.state)
        return _profile(configured)

    async def test_config(self, payload: DatabaseConfigUpdate) -> DatabaseConnectionRead:
        _validate_config_payload(payload)
        requested = self.requested_config()
        config = _config_with_overrides(requested, payload)
        return await self.inspect_config(
            config,
            verify_permissions=True,
            create_if_missing=True,
        )

    async def inspect_config(
        self,
        config: CloudraveConfig,
        *,
        verify_permissions: bool = False,
        create_if_missing: bool = False,
    ) -> DatabaseConnectionRead:
        engine = make_async_engine(config)
        try:
            result = await self.inspect_engine(
                engine,
                config.database.backend,
                verify_permissions=verify_permissions,
                database_name=_database_name(config),
            )
        finally:
            await engine.dispose()

        if (
            config.database.backend == "postgresql"
            and not result.reachable
            and result.error_code in {"connection_failed", "unknown"}
        ):
            # On Windows, asyncpg/PostgreSQL can close the socket rather than
            # report SQLSTATE 3D000 when the requested database is absent.
            # Check the server catalog before deciding whether to create it.
            try:
                exists = await _postgresql_database_exists(config)
            except (OSError, SQLAlchemyError, ValueError, PostgresError):
                exists = None
            if exists is False:
                result = DatabaseConnectionRead(
                    reachable=False,
                    backend="postgresql",
                    error_code="database_not_found",
                    error=f"目标数据库“{_database_name(config)}”不存在",
                )

        if (
            result.reachable
            or not create_if_missing
            or config.database.backend == "sqlite"
            or result.error_code != "database_not_found"
        ):
            return result

        try:
            created = await _create_remote_database(config)
        except (OSError, SQLAlchemyError, ValueError, PostgresError) as exc:
            error_code = _connection_error_code(exc)
            if error_code not in {"authentication_failed", "connection_failed"}:
                error_code = "database_creation_failed"
            return DatabaseConnectionRead(
                reachable=False,
                backend=config.database.backend,
                error_code=error_code,
                error=_database_creation_error_message(
                    exc,
                    config.database.backend,
                    _database_name(config),
                ),
            )

        retry_engine = make_async_engine(config)
        try:
            result = await self.inspect_engine(
                retry_engine,
                config.database.backend,
                verify_permissions=verify_permissions,
                database_name=_database_name(config),
            )
        finally:
            await retry_engine.dispose()
        if result.reachable:
            result.database_created = created
        return result

    async def inspect_engine(
        self,
        engine: AsyncEngine,
        backend: DatabaseBackend,
        *,
        verify_permissions: bool = False,
        database_name: str | None = None,
    ) -> DatabaseConnectionRead:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                if verify_permissions:
                    await _probe_permissions(connection)
                version = await _server_version(connection, backend)
                summary = await _data_summary(connection)
                schema_version = await _schema_version(connection)
                instance_id = await _database_instance_id(connection)
            return DatabaseConnectionRead(
                reachable=True,
                backend=backend,
                server_version=version,
                schema_version=schema_version,
                instance_id=instance_id,
                summary=summary,
            )
        except (OSError, SQLAlchemyError, ValueError, PostgresError) as exc:
            return DatabaseConnectionRead(
                reachable=False,
                backend=backend,
                error_code=_connection_error_code(exc),
                error=_connection_error_message(exc, backend, database_name),
            )

    async def start_migration(
        self,
        dep: "Dep",
        current_user: "User",
        *,
        data_choice: Literal["source", "target"],
        current_password: str,
        confirmation: str,
    ) -> DatabaseMigrationRead:
        if self.managed_by() != "config":
            raise HTTPException(status_code=409, detail="Database switching is disabled by a connection override")
        if self.state.status == "activation_pending":
            raise HTTPException(status_code=409, detail="当前迁移已完成，请先重启后端激活目标数据库")
        if self.state.status == "migrating" and (self._task is None or self._task.done()):
            raise HTTPException(status_code=409, detail="检测到未恢复的迁移任务，请先刷新数据库状态")
        requested = self.requested_config()
        source_backend = dep.config.database.backend
        target_backend = requested.database.backend
        if source_backend == target_backend:
            raise HTTPException(status_code=409, detail="No cross-database switch is pending")
        if not dep.password_service.verify_password(current_password, current_user.password_hash):
            raise HTTPException(status_code=403, detail="管理员密码不正确")
        expected = _database_name(database_config_for_backend(requested, target_backend))
        if confirmation.strip() != expected:
            raise HTTPException(status_code=422, detail=f"请输入目标数据库名称 {expected} 以确认")
        if self._task and not self._task.done():
            raise HTTPException(status_code=409, detail="A database migration is already running")
        if data_choice == "target":
            target_probe = await self.inspect_config(
                database_config_for_backend(requested, target_backend),
                verify_permissions=True,
            )
            target_users = next(
                (item.rows for item in target_probe.summary.tables if item.table == "users"),
                0,
            )
            if not target_probe.reachable:
                raise HTTPException(status_code=409, detail=target_probe.error or "目标数据库连接失败")
            if target_probe.schema_version is None or target_users < 1:
                raise HTTPException(
                    status_code=409,
                    detail="目标数据库没有可接管的 Cloudrave 用户数据，请选择迁移当前库数据",
                )
            if target_probe.instance_id and target_probe.instance_id != self.state.instance_id:
                raise HTTPException(
                    status_code=409,
                    detail="目标数据库属于另一个 Cloudrave 实例，不能直接接管；请选择迁移当前库数据",
                )

        job = MigrationJob(
            id=str(uuid4()),
            source_backend=source_backend,
            target_backend=target_backend,
            data_choice=data_choice,
        )
        self._current_job = job
        self.maintenance = True
        self.state.status = "migrating"
        self.state.requested_backend = target_backend
        self.state.migration_id = job.id
        self.state.last_error = None
        save_switch_state(self.root_dir, self.state)
        self._persist_job(job)
        self._log(job, "info", f"已创建 {source_backend} → {target_backend} 数据库迁移任务")
        self._task = asyncio.create_task(self._run_job(dep, job), name=f"database-migration-{job.id}")
        return self._migration_read(job)

    def migration(self, migration_id: str, *, after_seq: int = 0) -> DatabaseMigrationRead:
        job = self._current_job if self._current_job and self._current_job.id == migration_id else self._load_job(migration_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Database migration not found")
        return self._migration_read(job, after_seq=after_seq)

    def backup_path(self, migration_id: str) -> Path:
        job = self._current_job if self._current_job and self._current_job.id == migration_id else self._load_job(migration_id)
        if job is None or not job.backup_path:
            raise HTTPException(status_code=404, detail="Migration backup not found")
        path = Path(job.backup_path).resolve()
        backup_root = (self.root_dir / "data" / BACKUP_DIR).resolve()
        if backup_root not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="Migration backup not found")
        return path

    async def _run_job(self, dep: "Dep", job: MigrationJob) -> None:
        async with self._job_lock:
            target_engine: AsyncEngine | None = None
            workers_stopped = False
            try:
                await self.wait_for_business_writes()
                if job.data_choice == "source":
                    await dep.upload_cleanup_worker.stop()
                    await dep.gc_worker.stop()
                    workers_stopped = True
                self._set(job, phase="connecting", progress=5, detail="正在连接目标数据库")
                target_config = self.config_for(job.target_backend)
                target_engine = make_async_engine(target_config)
                target_probe = await self.inspect_engine(
                    target_engine,
                    job.target_backend,
                    verify_permissions=True,
                )
                if not target_probe.reachable:
                    raise RuntimeError(target_probe.error or "目标数据库连接失败")
                self._log(job, "info", f"目标 {job.target_backend} 连接成功")

                self._set(job, phase="inventory", progress=12, detail="正在盘点源库与目标库数据")
                source_probe = await self.inspect_engine(dep.engine, job.source_backend)
                if not source_probe.reachable:
                    raise RuntimeError(source_probe.error or "原数据库连接失败")
                job.total_rows = source_probe.summary.total_rows
                self._persist_job(job)
                self._log(
                    job,
                    "info",
                    f"原库共 {source_probe.summary.total_rows} 行业务数据，目标库共 {target_probe.summary.total_rows} 行",
                )

                if job.data_choice == "target":
                    self._log(job, "warning", "已选择保留目标库数据；原数据库不会被修改")
                    self._ready_for_restart(job)
                    return

                await asyncio.sleep(0)

                if target_probe.summary.has_data:
                    self._set(job, phase="backup", progress=18, detail="正在备份目标库现有数据")
                    job.backup_path = str(await self._backup_target(target_engine, job))
                    self._persist_job(job)
                    self._log(job, "info", f"目标库备份已完成：{Path(job.backup_path).name}")

                self._set(job, phase="schema", progress=25, detail="正在准备目标数据库结构")
                await create_schema(target_engine)
                self._log(job, "info", "目标数据库结构已更新到当前版本")

                self._set(job, phase="copying", progress=30, detail="正在复制数据库记录")
                await self._copy_database(dep.engine, target_engine, job)
                self._ready_for_restart(job)
            except Exception as exc:
                failed_table = job.current_table
                message = _migration_error_message(exc, failed_table)
                self._log(job, "error", message)
                if failed_table:
                    self._log(job, "warning", f"目标事务已回滚，未完成的 {failed_table} 写入未保留")
                self._set(
                    job,
                    phase="failed",
                    progress=job.progress,
                    current_table=None,
                    detail="迁移失败，原数据库仍在使用",
                    error=message,
                )
                self.state.status = "pending"
                self.state.requested_backend = job.target_backend
                self.state.last_error = message
                save_switch_state(self.root_dir, self.state)
            finally:
                self.maintenance = False
                if workers_stopped:
                    dep.gc_worker.start()
                    dep.upload_cleanup_worker.start()
                if target_engine is not None:
                    await target_engine.dispose()

    def _ready_for_restart(self, job: MigrationJob) -> None:
        self.state.status = "activation_pending"
        self.state.requested_backend = job.target_backend
        self.state.migration_id = job.id
        self.state.last_error = None
        save_switch_state(self.root_dir, self.state)
        self._log(job, "info", "迁移准备完成；请手动重启 Cloudrave 后端服务")
        self._set(
            job,
            phase="ready",
            progress=100,
            current_table=None,
            detail="数据已准备完成，等待手动重启后端",
            restart_required=True,
        )

    async def _backup_target(self, engine: AsyncEngine, job: MigrationJob) -> Path:
        directory = self.root_dir / "data" / BACKUP_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{job.id}-{job.target_backend}.jsonl.gz"
        async with engine.connect() as connection:
            summary = await _data_summary(connection)
            content_digest = hashlib.sha256()
            table_digests: dict[str, str] = {}
            with gzip.open(path, "wt", encoding="utf-8") as output:
                output.write(json.dumps({
                    "type": "cloudrave-database-backup",
                    "version": 1,
                    "backend": job.target_backend,
                    "created_at": datetime.now(UTC).isoformat(),
                    "tables": {item.table: item.rows for item in summary.tables},
                }, ensure_ascii=False) + "\n")
                for table in _application_tables():
                    if not any(item.table == table.name for item in summary.tables):
                        continue
                    order = list(table.primary_key.columns)
                    result = await connection.execute(select(table).order_by(*order))
                    table_digest = hashlib.sha256()
                    for row in result.mappings():
                        line = json.dumps({
                            "table": table.name,
                            "row": {key: _json_value(value) for key, value in row.items()},
                        }, ensure_ascii=False, sort_keys=True) + "\n"
                        encoded = line.encode("utf-8")
                        table_digest.update(encoded)
                        content_digest.update(encoded)
                        output.write(line)
                    table_digests[table.name] = table_digest.hexdigest()
                output.write(json.dumps({
                    "type": "manifest-checksum",
                    "algorithm": "sha256",
                    "content_sha256": content_digest.hexdigest(),
                    "table_sha256": table_digests,
                }, ensure_ascii=False, sort_keys=True) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    async def _copy_database(
        self,
        source_engine: AsyncEngine,
        target_engine: AsyncEngine,
        job: MigrationJob,
    ) -> None:
        source = await source_engine.connect()
        try:
            if job.source_backend == "sqlite":
                await source.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                source = await source.execution_options(isolation_level="REPEATABLE READ")
                await source.begin()

            reference_values = await _source_foreign_key_values(source)
            reference_repairs: dict[tuple[str, str, str], int] = {}
            source_hashes: dict[str, str] = {}
            async with target_engine.begin() as target:
                for table in reversed(_application_tables()):
                    await target.execute(delete(table))

                processed = 0
                total = max(job.total_rows, 1)
                for table in _application_tables():
                    job.current_table = table.name
                    self._log(job, "info", f"开始复制 {table.name}")
                    digest = _RowDigest()
                    offset = 0
                    order = list(table.primary_key.columns)
                    statement = select(table).order_by(*order)
                    if _self_referential_columns(table):
                        result = await source.execute(statement)
                        source_rows = []
                        for row in result.mappings():
                            normalized = _normalize_row_for_target(table, dict(row), job.target_backend)
                            repaired = _repair_source_foreign_keys(
                                table, normalized, reference_values, reference_repairs
                            )
                            if repaired is not None:
                                source_rows.append(repaired)
                        # Parent-first insertion changes the row order; the
                        # content digest is intentionally order independent.
                        for row in source_rows:
                            digest.update(row)
                        ordered_rows = _parent_first_rows(table, source_rows)
                        partitions = (
                            ordered_rows[index:index + COPY_BATCH_SIZE]
                            for index in range(0, len(ordered_rows), COPY_BATCH_SIZE)
                        )
                        for values in partitions:
                            await target.execute(insert(table), values)
                            offset += len(values)
                            processed += len(values)
                            job.processed_rows = processed
                            job.progress = min(80, 30 + int(processed / total * 50))
                            job.detail = f"正在复制 {table.name}：{offset} 行"
                            self._persist_job(job)
                    else:
                        async with source.stream(
                            statement.execution_options(yield_per=COPY_BATCH_SIZE)
                        ) as result:
                            async for rows in result.mappings().partitions(COPY_BATCH_SIZE):
                                values = []
                                for row in rows:
                                    normalized = _normalize_row_for_target(
                                        table, dict(row), job.target_backend
                                    )
                                    repaired = _repair_source_foreign_keys(
                                        table, normalized, reference_values, reference_repairs
                                    )
                                    if repaired is not None:
                                        values.append(repaired)
                                if not values:
                                    continue
                                await target.execute(insert(table), values)
                                for row in values:
                                    digest.update(row)
                                offset += len(values)
                                processed += len(values)
                                job.processed_rows = processed
                                job.progress = min(80, 30 + int(processed / total * 50))
                                job.detail = f"正在复制 {table.name}：{offset} 行"
                                self._persist_job(job)
                    source_hashes[table.name] = digest.hexdigest()
                    self._log(job, "info", f"{table.name} 已复制 {offset} 行")

                for (table_name, column_name, action), count in sorted(reference_repairs.items()):
                    if action == "null":
                        message = f"{table_name}.{column_name} 有 {count} 条失效外键，已仅在目标库置空；原库未修改"
                    else:
                        message = f"{table_name}.{column_name} 有 {count} 条已失效的级联子记录，目标库已跳过；原库未修改"
                    self._log(job, "warning", message)

                self._set(job, phase="verifying", progress=82, detail="正在校验目标数据库")
                for index, table in enumerate(_application_tables(), start=1):
                    job.current_table = table.name
                    digest = _RowDigest()
                    order = list(table.primary_key.columns)
                    result = await target.execute(select(table).order_by(*order))
                    for row in result.mappings():
                        digest.update(dict(row))
                    if digest.hexdigest() != source_hashes[table.name]:
                        raise RuntimeError(f"表 {table.name} 的迁移校验不一致")
                    job.progress = 82 + int(index / len(_application_tables()) * 13)
                    self._persist_job(job)
                await _reset_sequences(target, job.target_backend)
                meta = Base.metadata.tables["system_meta"]
                await target.execute(delete(meta).where(meta.c.key.in_([
                    "database.instance_id", "database.migration_id"
                ])))
                await target.execute(insert(meta), [
                    {
                        "key": "database.instance_id",
                        "value": self.state.instance_id,
                        "updated_at": datetime.now(UTC),
                    },
                    {
                        "key": "database.migration_id",
                        "value": job.id,
                        "updated_at": datetime.now(UTC),
                    },
                ])
            self._log(job, "info", "所有业务表的行数与内容摘要校验通过")
        finally:
            if source.in_transaction():
                await source.rollback()
            await source.close()

    def _set(self, job: MigrationJob, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = datetime.now(UTC).isoformat()
        self._persist_job(job)

    async def _recover_interrupted_job_state(self, job: MigrationJob) -> None:
        committed = False
        engine: AsyncEngine | None = None
        try:
            engine = make_async_engine(self.config_for(job.target_backend))
            async with engine.connect() as connection:
                names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
                if "system_meta" in names:
                    meta = Base.metadata.tables["system_meta"]
                    marker = await connection.scalar(
                        select(meta.c.value).where(meta.c.key == "database.migration_id")
                    )
                    committed = marker == job.id
        except (OSError, SQLAlchemyError, ValueError):
            committed = False
        finally:
            if engine is not None:
                await engine.dispose()

        if committed:
            message = "检测到目标事务已完整提交，已恢复为等待重启激活状态"
            self._log(job, "warning", message)
            self._set(job, phase="ready", progress=100, detail=message, restart_required=True)
            self.state.status = "activation_pending"
            self.state.requested_backend = job.target_backend
            self.state.migration_id = job.id
            self.state.last_error = None
        else:
            message = "后端在迁移完成前重新启动；目标事务已回滚，请重新发起迁移"
            self._log(job, "error", message)
            self._set(job, phase="failed", current_table=None, detail=message, error=message)
            self.state.status = "pending"
            self.state.requested_backend = job.target_backend
            self.state.last_error = message
        save_switch_state(self.root_dir, self.state)

    def _log(self, job: MigrationJob, level: str, message: str) -> None:
        job.log_seq += 1
        entry = {
            "seq": job.log_seq,
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "message": _sanitize_message(message)[:LOG_MESSAGE_LIMIT],
        }
        path = self._log_path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._persist_job(job)

    def _migration_read(self, job: MigrationJob, *, after_seq: int = 0) -> DatabaseMigrationRead:
        logs: list[DatabaseMigrationLogRead] = []
        path = self._log_path(job.id)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    raw = json.loads(line)
                    if int(raw["seq"]) > after_seq:
                        raw["message"] = _public_migration_message(
                            str(raw.get("message", "")),
                            job.current_table,
                        )
                        logs.append(DatabaseMigrationLogRead(**raw))
                except (ValueError, TypeError, KeyError):
                    continue
        backup = Path(job.backup_path) if job.backup_path else None
        return DatabaseMigrationRead(
            id=job.id,
            source_backend=job.source_backend,
            target_backend=job.target_backend,
            data_choice=job.data_choice,
            phase=job.phase,  # type: ignore[arg-type]
            progress=job.progress,
            current_table=None if job.phase == "failed" else job.current_table,
            processed_rows=job.processed_rows,
            total_rows=job.total_rows,
            detail=_public_migration_message(job.detail, job.current_table),
            restart_required=job.restart_required,
            backup_available=bool(backup and backup.is_file()),
            backup_name=backup.name if backup else None,
            error=_public_migration_message(job.error, job.current_table) if job.error else None,
            created_at=datetime.fromisoformat(job.created_at),
            updated_at=datetime.fromisoformat(job.updated_at),
            logs=logs,
        )

    def _job_path(self, migration_id: str) -> Path:
        return self.root_dir / "data" / MIGRATION_DIR / f"{migration_id}.json"

    def _log_path(self, migration_id: str) -> Path:
        return self.root_dir / "data" / MIGRATION_DIR / f"{migration_id}.jsonl"

    def _persist_job(self, job: MigrationJob) -> None:
        path = self._job_path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        job.updated_at = datetime.now(UTC).isoformat()
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)

    def _load_job(self, migration_id: str) -> MigrationJob | None:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", migration_id):
            return None
        path = self._job_path(migration_id)
        if not path.is_file():
            return None
        try:
            return MigrationJob(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _load_latest_job(self) -> MigrationJob | None:
        jobs = self._load_recent_jobs(limit=1)
        return jobs[0] if jobs else None

    def _load_recent_jobs(self, *, limit: int) -> list[MigrationJob]:
        directory = self.root_dir / "data" / MIGRATION_DIR
        if not directory.is_dir():
            return []
        jobs: list[MigrationJob] = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                jobs.append(MigrationJob(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
            if len(jobs) >= limit:
                break
        return jobs


def _application_tables():
    import models  # noqa: F401

    return [table for table in Base.metadata.sorted_tables if table.name not in INTERNAL_TABLES]


async def _data_summary(connection: AsyncConnection) -> DatabaseDataSummary:
    table_names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    tables: list[DatabaseTableSummary] = []
    total = 0
    for table in _application_tables():
        if table.name not in table_names:
            continue
        rows = int(await connection.scalar(select(func.count()).select_from(table)) or 0)
        tables.append(DatabaseTableSummary(table=table.name, rows=rows))
        total += rows
    meaningful_rows = total
    if "system_meta" in table_names:
        meta = Base.metadata.tables["system_meta"]
        internal_rows = int(await connection.scalar(
            select(func.count()).select_from(meta).where(
                ((meta.c.key == "auth.first_admin_user_id") & (meta.c.value == ""))
                | meta.c.key.in_(["database.instance_id", "database.migration_id"])
            )
        ) or 0)
        meaningful_rows = max(0, meaningful_rows - internal_rows)
    return DatabaseDataSummary(total_rows=total, has_data=meaningful_rows > 0, tables=tables)


async def _schema_version(connection: AsyncConnection) -> int | None:
    names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    if "schema_migrations" not in names:
        return None
    return await connection.scalar(text("SELECT max(version) FROM schema_migrations"))


async def _database_instance_id(connection: AsyncConnection) -> str | None:
    names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
    if "system_meta" not in names:
        return None
    meta = Base.metadata.tables["system_meta"]
    value = await connection.scalar(
        select(meta.c.value).where(meta.c.key == "database.instance_id")
    )
    return str(value) if value else None


async def _server_version(connection: AsyncConnection, backend: DatabaseBackend) -> str:
    statement = "SELECT sqlite_version()" if backend == "sqlite" else "SELECT version()"
    return str(await connection.scalar(text(statement)) or "")[:255]


async def _probe_permissions(connection: AsyncConnection) -> None:
    """Exercise the temporary-table DDL and write privileges migration needs."""

    table = "cloudrave_connection_permission_test"
    await connection.exec_driver_sql(f"CREATE TEMPORARY TABLE {table} (id INTEGER NOT NULL)")
    try:
        await connection.exec_driver_sql(f"INSERT INTO {table} (id) VALUES (1)")
        await connection.exec_driver_sql(f"DELETE FROM {table} WHERE id = 1")
    finally:
        await connection.exec_driver_sql(f"DROP TABLE {table}")


async def _reset_sequences(connection: AsyncConnection, backend: DatabaseBackend) -> None:
    integer_tables = [
        table
        for table in _application_tables()
        if len(table.primary_key.columns) == 1
        and next(iter(table.primary_key.columns)).type.python_type is int
    ]
    if backend == "postgresql":
        for table in integer_tables:
            column = next(iter(table.primary_key.columns))
            await connection.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table.name}', '{column.name}'), "
                f"COALESCE(MAX({column.name}), 1), MAX({column.name}) IS NOT NULL) FROM {table.name}"
            ))
    elif backend == "mysql":
        # Explicit positive AUTO_INCREMENT values advance InnoDB's counter.
        # Touching ALTER TABLE here would implicitly commit the copy transaction.
        return
    elif backend == "sqlite":
        names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
        if "sqlite_sequence" not in names:
            return
        for table in integer_tables:
            column = next(iter(table.primary_key.columns))
            maximum = int(await connection.scalar(select(func.max(column))) or 0)
            await connection.execute(
                text("UPDATE sqlite_sequence SET seq = :seq WHERE name = :name"),
                {"seq": maximum, "name": table.name},
            )


def _profile(config: CloudraveConfig) -> DatabaseProfileRead:
    backend = config.database.backend
    if backend == "sqlite":
        return DatabaseProfileRead(
            backend=backend,
            name=config.database.sqlite_path,
            password_configured=False,
        )
    url = make_url(config.database.url)
    return DatabaseProfileRead(
        backend=backend,
        host=url.host,
        port=url.port or (3306 if backend == "mysql" else 5432),
        user=url.username,
        name=url.database or "cloudrave",
        password_configured=bool(url.password),
    )


def _config_with_overrides(config: CloudraveConfig, payload: DatabaseConfigUpdate) -> CloudraveConfig:
    database = config.database
    prefix = "mysql" if payload.backend == "mysql" else "postgresql"
    changes: dict[str, Any] = {}
    if payload.backend == "sqlite":
        if payload.sqlite_path:
            changes["sqlite_path"] = payload.sqlite_path.strip()
    else:
        if payload.host is not None:
            changes[f"{prefix}_host"] = payload.host.strip()
        if payload.port is not None:
            changes[f"{prefix}_port"] = payload.port
        if payload.user is not None:
            changes[f"{prefix}_user"] = payload.user.strip()
        if payload.password:
            changes[f"{prefix}_password"] = payload.password
        if payload.name is not None:
            changes[f"{prefix}_name"] = payload.name.strip()
    temporary = replace(config, database=replace(database, **changes))
    return database_config_for_backend(temporary, payload.backend)


def _validate_config_payload(payload: DatabaseConfigUpdate) -> None:
    if payload.backend == "sqlite":
        if payload.sqlite_path is not None and not payload.sqlite_path.strip():
            raise HTTPException(status_code=422, detail="SQLite 文件路径不能为空")
        return
    required = {
        "主机": payload.host,
        "用户名": payload.user,
        "数据库名": payload.name,
    }
    missing = [label for label, value in required.items() if value is not None and not value.strip()]
    if missing:
        raise HTTPException(status_code=422, detail=f"{', '.join(missing)}不能为空")
    if payload.name is not None:
        maximum = 64 if payload.backend == "mysql" else 63
        if len(payload.name.strip()) > maximum:
            raise HTTPException(status_code=422, detail=f"数据库名不能超过 {maximum} 个字符")
        if "\x00" in payload.name:
            raise HTTPException(status_code=422, detail="数据库名包含无效字符")


def _database_name(config: CloudraveConfig) -> str:
    if config.database.backend == "sqlite":
        return Path(make_url(config.database.url).database or config.database.sqlite_path).name
    return make_url(config.database.url).database or "cloudrave"


def _self_referential_columns(table) -> list:
    return [
        column
        for column in table.columns
        if any(foreign_key.column.table is table for foreign_key in column.foreign_keys)
    ]


async def _source_foreign_key_values(source: AsyncConnection) -> dict[Any, set[Any]]:
    referenced = {
        foreign_key.column
        for table in _application_tables()
        for column in table.columns
        for foreign_key in column.foreign_keys
    }
    values: dict[Any, set[Any]] = {}
    for column in referenced:
        result = await source.execute(select(column))
        values[column] = set(result.scalars())
    return values


def _repair_source_foreign_keys(
    table,
    row: dict[str, Any],
    reference_values: dict[Any, set[Any]],
    repairs: dict[tuple[str, str, str], int],
) -> dict[str, Any] | None:
    for column in table.columns:
        for foreign_key in column.foreign_keys:
            value = row[column.name]
            if value is None or value in reference_values[foreign_key.column]:
                continue
            if column.nullable:
                row[column.name] = None
                key = (table.name, column.name, "null")
            elif foreign_key.ondelete and foreign_key.ondelete.upper() == "CASCADE":
                key = (table.name, column.name, "skip")
                repairs[key] = repairs.get(key, 0) + 1
                return None
            else:
                raise RuntimeError(
                    f"原数据库 {table.name}.{column.name} 包含失效的必填外键，"
                    "无法安全迁移；请先修复源数据库记录"
                )
            repairs[key] = repairs.get(key, 0) + 1
    return row


def _parent_first_rows(table, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Topologically order single-primary-key self-referential table rows."""
    self_columns = _self_referential_columns(table)
    primary_keys = list(table.primary_key.columns)
    if not self_columns or not rows:
        return rows
    if len(primary_keys) != 1:
        raise RuntimeError(f"表 {table.name} 使用了暂不支持的复合自引用主键")

    primary_name = primary_keys[0].name
    pending = {row[primary_name]: row for row in rows}
    ordered: list[dict[str, Any]] = []
    resolved: set[Any] = set()
    while pending:
        ready = [
            (identity, row)
            for identity, row in pending.items()
            if all(
                row[column.name] is None
                or row[column.name] in resolved
                or row[column.name] not in pending
                for column in self_columns
            )
        ]
        if not ready:
            raise RuntimeError(f"表 {table.name} 包含循环自引用，无法安全迁移")
        ready.sort(key=lambda item: repr(item[0]))
        for identity, row in ready:
            ordered.append(row)
            resolved.add(identity)
            del pending[identity]
    return ordered


async def _create_remote_database(config: CloudraveConfig) -> bool:
    """Create a missing MySQL/PostgreSQL database using a server-level connection."""
    backend = config.database.backend
    if backend == "sqlite":
        return False
    target_name = _database_name(config)
    maintenance_config = _server_level_config(config)
    engine = make_async_engine(maintenance_config)
    try:
        async with engine.connect() as base_connection:
            connection = await base_connection.execution_options(isolation_level="AUTOCOMMIT")
            identifier = connection.dialect.identifier_preparer.quote(target_name)
            statement = (
                f"CREATE DATABASE {identifier} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                if backend == "mysql"
                else f"CREATE DATABASE {identifier} ENCODING 'UTF8'"
            )
            try:
                await connection.exec_driver_sql(statement)
            except (SQLAlchemyError, PostgresError) as exc:
                if _is_duplicate_database_error(exc):
                    return False
                raise
    finally:
        await engine.dispose()
    return True


async def _postgresql_database_exists(config: CloudraveConfig) -> bool:
    engine = make_async_engine(_server_level_config(config))
    try:
        async with engine.connect() as connection:
            return bool(await connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :name)"),
                {"name": _database_name(config)},
            ))
    finally:
        await engine.dispose()


def _server_level_config(config: CloudraveConfig) -> CloudraveConfig:
    backend = config.database.backend
    target_url = make_url(config.database.url)
    maintenance_database = None if backend == "mysql" else "postgres"
    # URL.set(database=None) means "leave unchanged" in SQLAlchemy. Build a
    # fresh URL so MySQL really connects at server scope without selecting the
    # database that we are about to create.
    maintenance_url = URL.create(
        drivername=target_url.drivername,
        username=target_url.username,
        password=target_url.password,
        host=target_url.host,
        port=target_url.port,
        database=maintenance_database,
        query=target_url.query,
    )
    return replace(
        config,
        database=replace(
            config.database,
            url=maintenance_url.render_as_string(hide_password=False),
        ),
    )


def _exception_values(exc: BaseException) -> list[BaseException]:
    values: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop(0)
        if current is None or current in values:
            continue
        values.append(current)
        original = getattr(current, "orig", None)
        pending.extend((original if isinstance(original, BaseException) else None, current.__cause__, current.__context__))
    return values


def _driver_codes(exc: BaseException) -> set[str]:
    codes: set[str] = set()
    for current in _exception_values(exc):
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if value is not None:
                codes.add(str(value))
        if current.args and isinstance(current.args[0], (int, str)):
            codes.add(str(current.args[0]))
    return codes


def _connection_error_code(exc: BaseException) -> str:
    codes = _driver_codes(exc)
    message = " ".join(str(item) for item in _exception_values(exc)).lower()
    if codes & {"1049", "3D000"} or "unknown database" in message or "does not exist" in message and "database" in message:
        return "database_not_found"
    if codes & {"1045", "28P01"} or "access denied for user" in message or "password authentication failed" in message:
        return "authentication_failed"
    if codes & {"1044", "1142", "42501"} or "permission denied" in message or "insufficient privilege" in message:
        return "permission_denied"
    if codes & {"08003", "08006", "57P03"} or any(token in message for token in ("connection refused", "can't connect", "cannot connect", "timed out", "timeout")):
        return "connection_failed"
    return "unknown"


def _is_duplicate_database_error(exc: BaseException) -> bool:
    codes = _driver_codes(exc)
    message = " ".join(str(item) for item in _exception_values(exc)).lower()
    return bool(codes & {"1007", "42P04"} or "database exists" in message or "already exists" in message)


def _connection_error_message(
    exc: BaseException,
    backend: DatabaseBackend,
    database_name: str | None,
) -> str:
    code = _connection_error_code(exc)
    label = "MySQL" if backend == "mysql" else "PostgreSQL" if backend == "postgresql" else "SQLite"
    if code == "database_not_found":
        return f"目标数据库“{database_name or 'cloudrave'}”不存在"
    if code == "authentication_failed":
        return f"{label} 用户名或密码错误，请检查连接信息"
    if code == "permission_denied":
        return f"当前 {label} 用户权限不足，请授予建库、建表和读写权限后重试"
    if code == "connection_failed":
        return f"无法连接 {label} 服务，请检查主机、端口及服务运行状态"
    return f"{label} 连接测试失败，请检查连接配置"


def _database_creation_error_message(
    exc: BaseException,
    backend: DatabaseBackend,
    database_name: str,
) -> str:
    code = _connection_error_code(exc)
    if code in {"authentication_failed", "connection_failed"}:
        return _connection_error_message(exc, backend, database_name)
    return (
        f"目标数据库“{database_name}”不存在，Cloudrave 自动创建失败。"
        "请为当前数据库用户授予 CREATE DATABASE 权限后重试"
    )


def _migration_error_message(exc: BaseException, table: str | None) -> str:
    codes = _driver_codes(exc)
    raw = " ".join(str(item) for item in _exception_values(exc)).lower()
    table_hint = f"（表：{table}）" if table else ""
    if codes & {"1406", "22001"} or "data too long" in raw or "string data right truncation" in raw:
        return f"目标数据库字段长度不足{table_hint}，目标事务已回滚；请升级数据库结构后重试"
    if isinstance(exc, SQLAlchemyError):
        return f"写入目标数据库失败{table_hint}，目标事务已回滚；原数据库仍在使用"
    message = _sanitize_message(str(exc) or exc.__class__.__name__)
    return message or f"数据库迁移失败{table_hint}"


def _public_migration_message(value: str, table: str | None) -> str:
    raw = value.lower()
    table_hint = f"（表：{table}）" if table else ""
    if "data too long" in raw or "string data right truncation" in raw:
        return f"目标数据库字段长度不足{table_hint}，目标事务已回滚；请升级数据库结构后重试"
    if "[sql:" in raw or "[parameters:" in raw:
        return f"写入目标数据库失败{table_hint}，目标事务已回滚；原数据库仍在使用"
    return _sanitize_message(value)


def _canonical_row(row: dict[str, Any]) -> bytes:
    normalized = {key: _digest_value(value) for key, value in row.items()}
    return (json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _digest_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_digest_value(item) for item in value]
    if isinstance(value, float):
        # MySQL JSON stores a binary double but can emit a slightly different
        # final decimal digit on readback. Sixteen significant digits preserve
        # meaningful double precision while allowing stable cross-dialect hashes.
        return {"$float": format(value, ".16g")}
    return _json_value(value)


def _normalize_row_for_target(table, row: dict[str, Any], backend: DatabaseBackend) -> dict[str, Any]:
    normalized = dict(row)
    for column in table.columns:
        value = normalized.get(column.name)
        if isinstance(column.type, DateTime) and isinstance(value, datetime):
            aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            normalized[column.name] = aware if backend == "postgresql" else aware.replace(tzinfo=None)
    return normalized


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return {"$datetime": aware.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, Enum):
        return value.value
    return value


def _sanitize_message(value: str) -> str:
    sanitized = re.sub(r"(?i)(password|passwd|pwd)=([^&\s]+)", r"\1=***", value)
    sanitized = re.sub(r"(?i)(://[^:/\s]+:)([^@\s]+)(@)", r"\1***\3", sanitized)
    sanitized = re.sub(r"(?is)\s*\[SQL:.*$", "", sanitized)
    sanitized = re.sub(r"(?is)\s*\[parameters:.*$", "", sanitized)
    sanitized = re.sub(r"(?is)\s*\(Background on this error at:.*$", "", sanitized)
    return sanitized.strip()[:LOG_MESSAGE_LIMIT]


def _update_ini_section_atomic(path: Path, section: str, updates: dict[str, str]) -> None:
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = original.splitlines()
    section_header = f"[{section.lower()}]"
    start = next((index for index, line in enumerate(lines) if line.strip().lower() == section_header), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        start = len(lines) - 1
        end = len(lines)
    else:
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].strip().startswith("[")),
            len(lines),
        )

    remaining = dict(updates)
    key_pattern = re.compile(r"^\s*([^#;][^=]*?)\s*=.*$")
    for index in range(start + 1, end):
        match = key_pattern.match(lines[index])
        if not match:
            continue
        key = match.group(1).strip().lower()
        if key in remaining:
            lines[index] = f"{key} = {remaining.pop(key)}"
    for key, value in remaining.items():
        lines.insert(end, f"{key} = {value}")
        end += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)

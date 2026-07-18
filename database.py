"""Database setup and schema bootstrap helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import string

from sqlalchemy import BigInteger, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import mysql, postgresql, sqlite

from config import CloudraveConfig


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


def make_async_engine(config: CloudraveConfig) -> AsyncEngine:
    _ensure_sqlite_parent_dir(config)
    engine_kwargs: dict = {"echo": config.database.echo, "future": True}
    if config.database.backend == "sqlite":
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs.update(
            pool_size=config.database.pool_size,
            max_overflow=config.database.max_overflow,
            pool_pre_ping=config.database.pool_pre_ping,
            pool_recycle=config.database.pool_recycle,
        )
    engine = create_async_engine(config.database.url, **engine_kwargs)
    if config.database.backend == "sqlite":
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()
    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def make_sync_engine(config: CloudraveConfig):
    _ensure_sqlite_parent_dir(config)
    sync_url = (
        config.database.url.replace("+asyncpg", "+psycopg")
        .replace("+aiomysql", "+pymysql")
        .replace("+aiosqlite", "")
    )
    return create_engine(sync_url, echo=config.database.echo, future=True)


async def create_schema(engine: AsyncEngine) -> None:
    import app.models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_apply_runtime_migrations)


def write_schema_sql(config: CloudraveConfig) -> Path:
    import app.models  # noqa: F401

    output_path = config.root_dir / "db.sql"
    dialect = _get_sql_dialect(config)
    statements: list[str] = []

    for table in Base.metadata.sorted_tables:
        statements.append(f"{CreateTable(table).compile(dialect=dialect)};")

    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            statements.append(f"{CreateIndex(index).compile(dialect=dialect)};")

    output_path.write_text("\n\n".join(statements).strip() + "\n", encoding="utf-8")
    return output_path


@asynccontextmanager
async def session_scope(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def _get_sql_dialect(config: CloudraveConfig):
    backend = config.database.backend
    if backend == "postgresql":
        return postgresql.dialect()
    if backend == "mysql":
        return mysql.dialect()
    return sqlite.dialect()


def _ensure_sqlite_parent_dir(config: CloudraveConfig) -> None:
    if config.database.backend != "sqlite":
        return

    database_path = make_url(config.database.url).database
    if not database_path or database_path == ":memory:":
        return

    Path(database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _apply_runtime_migrations(connection) -> None:
    import app.models  # noqa: F401

    Base.metadata.create_all(connection, checkfirst=True)
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    _migrate_large_sizes(connection, inspector, tables)
    if "users" in tables:
        _migrate_users_table(connection, inspector)
    if "objects" in tables:
        _migrate_objects_table(connection, inspector)
    if "upload_sessions" in tables:
        _migrate_uploads_table(connection, inspector)
    if "share_mounts" in tables:
        _migrate_shared_table(connection, inspector)


def _migrate_users_table(connection, inspector) -> None:
    columns = {column["name"] for column in inspector.get_columns("users")}
    dialect = connection.dialect.name

    if "uid" not in columns:
        connection.execute(text(f"ALTER TABLE users ADD COLUMN uid {_varchar_type(dialect, 16)}"))
    if "avatar_path" not in columns:
        connection.execute(text(f"ALTER TABLE users ADD COLUMN avatar_path {_varchar_type(dialect, 512)}"))
    if "timezone" not in columns:
        connection.execute(text(f"ALTER TABLE users ADD COLUMN timezone {_varchar_type(dialect, 64)}"))
    if "password_updated_at" not in columns:
        connection.execute(text(f"ALTER TABLE users ADD COLUMN password_updated_at {_datetime_type(dialect)}"))
    if "totp_secret" not in columns:
        connection.execute(text(f"ALTER TABLE users ADD COLUMN totp_secret {_varchar_type(dialect, 255)}"))
    if "totp_enabled" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT 0"))
    if "totp_backup_codes" not in columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN totp_backup_codes TEXT DEFAULT '[]'"))

    rows = connection.execute(
        text("SELECT id, created_at, uid, timezone, password_updated_at FROM users ORDER BY id ASC")
    ).mappings()
    used_uids: set[str] = set()

    for row in rows:
        current_uid = row.get("uid")
        updates: dict[str, object] = {}

        if not current_uid:
            candidate = _generate_runtime_uid(used_uids)
            updates["uid"] = candidate
            used_uids.add(candidate)
        else:
            used_uids.add(str(current_uid))

        if row.get("password_updated_at") is None:
            updates["password_updated_at"] = row.get("created_at")
        if not row.get("timezone"):
            updates["timezone"] = "Asia/Shanghai"

        if updates:
            updates["id"] = row["id"]
            connection.execute(
                text(
                    "UPDATE users SET "
                    + ", ".join(f"{column} = :{column}" for column in updates if column != "id")
                    + " WHERE id = :id"
                ),
                updates,
            )

    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_uid" not in indexes:
        connection.execute(text("CREATE UNIQUE INDEX ix_users_uid ON users (uid)"))


def _migrate_large_sizes(connection, inspector, tables: set[str]) -> None:
    dialect = connection.dialect.name
    if dialect == "sqlite":
        return  # SQLite INTEGER already stores signed 64-bit values.
    targets = {
        "bucket_mounts": {"quota_bytes": True},
        "nodes": {"size": False},
        "upload_sessions": {"size": False, "part_size": False},
        "upload_parts": {"size": False},
    }
    for table, columns in targets.items():
        if table not in tables:
            continue
        existing = {column["name"]: column for column in inspector.get_columns(table)}
        for name, nullable in columns.items():
            column = existing.get(name)
            if column is None or isinstance(column["type"], BigInteger):
                continue
            if dialect == "postgresql":
                statement = f"ALTER TABLE {table} ALTER COLUMN {name} TYPE BIGINT"
            elif dialect == "mysql":
                statement = f"ALTER TABLE {table} MODIFY COLUMN {name} BIGINT {'NULL' if nullable else 'NOT NULL'}"
            else:
                continue
            connection.execute(text(statement))


def _migrate_objects_table(connection, inspector) -> None:
    columns = {column["name"] for column in inspector.get_columns("objects")}
    if "physical_id" not in columns:
        connection.execute(text(f"ALTER TABLE objects ADD COLUMN physical_id {_varchar_type(connection.dialect.name, 64)}"))
    indexes = {index["name"] for index in inspect(connection).get_indexes("objects")}
    if "ix_objects_physical_id" not in indexes:
        connection.execute(text("CREATE INDEX ix_objects_physical_id ON objects (physical_id)"))


def _migrate_shared_table(connection, inspector) -> None:
    columns = {column["name"] for column in inspector.get_columns("share_mounts")}
    if "grant_hash" not in columns:
        connection.execute(text(f"ALTER TABLE share_mounts ADD COLUMN grant_hash {_varchar_type(connection.dialect.name, 64)}"))


def _migrate_uploads_table(connection, inspector) -> None:
    columns = {column["name"] for column in inspector.get_columns("upload_sessions")}
    if "checksum" not in columns:
        connection.execute(
            text(f"ALTER TABLE upload_sessions ADD COLUMN checksum {_varchar_type(connection.dialect.name, 128)}")
        )


def _varchar_type(dialect: str, length: int) -> str:
    if dialect == "mysql":
        return f"VARCHAR({length})"
    return f"VARCHAR({length})"


def _datetime_type(dialect: str) -> str:
    if dialect == "postgresql":
        return "TIMESTAMP WITH TIME ZONE"
    if dialect == "mysql":
        return "DATETIME"
    return "DATETIME"


def _generate_runtime_uid(used_uids: set[str]) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(5))
        if candidate not in used_uids:
            return candidate

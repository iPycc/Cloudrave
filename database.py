"""Database setup and schema bootstrap helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import string

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import mysql, postgresql, sqlite

from config import CloudraveConfig


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


def make_async_engine(config: CloudraveConfig) -> AsyncEngine:
    _ensure_sqlite_parent_dir(config)
    return create_async_engine(config.database.url, echo=config.database.echo, future=True)


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
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "users" in tables:
        _migrate_users_table(connection, inspector)


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

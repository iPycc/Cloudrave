"""Database setup and schema bootstrap helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import mysql, postgresql, sqlite

from core.cfg import CloudraveConfig


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
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode")
                current_journal_mode = cursor.fetchone()[0]
                if str(current_journal_mode).lower() != "wal":
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
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
    import models  # noqa: F401
    from db.mig.run import run_migrations

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(run_migrations)


def write_schema_sql(config: CloudraveConfig) -> Path:
    import models  # noqa: F401

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

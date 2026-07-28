"""Bootstrap helpers for config, filesystem, and initial schema setup."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from core.cfg import CloudraveConfig, load_config
from db.base import create_schema, make_async_engine, make_session_factory, session_scope
from core.log import configure_logging, get_logger


LOGGER = get_logger(__name__)


async def run_bootstrap_async(root_dir: Path | None = None) -> CloudraveConfig:
    resolved_root = (root_dir or Path(__file__).resolve().parents[2]).resolve()
    config = load_config(resolved_root)
    configure_logging(config)
    _ensure_directories(config)

    engine = make_async_engine(config)
    try:
        await create_schema(engine)
        session_factory = make_session_factory(engine)
        async with session_scope(session_factory) as session:
            from models.tables import SystemMeta

            admin_state = await session.scalar(
                select(SystemMeta).where(SystemMeta.key == "auth.first_admin_user_id")
            )
            if admin_state is None:
                session.add(SystemMeta(key="auth.first_admin_user_id", value=""))
    finally:
        await engine.dispose()

    LOGGER.info("Bootstrap completed", extra={"request_id": "bootstrap", "ip": "-", "actor": "system"})
    return config


def run_bootstrap(root_dir: Path | None = None) -> CloudraveConfig:
    return asyncio.run(run_bootstrap_async(root_dir))


def _ensure_directories(config: CloudraveConfig) -> None:
    config.local_upload_root_path.mkdir(parents=True, exist_ok=True)

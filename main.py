"""Cloudrave backend entrypoint."""

from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn

from app import __version__
from app.dep import Dep
from app.factory import create_app
from config import load_config
from logging_config import LogOperation, LogOutcome, configure_logging, get_logger, print_startup_banner


ROOT_DIR = Path(__file__).resolve().parent
CONFIG = load_config(ROOT_DIR)
app = create_app(CONFIG)
LOGGER = get_logger("cloudrave.main")


async def preflight_startup() -> None:
    dep = Dep(CONFIG)
    try:
        await dep.check_database()
    finally:
        await dep.disconnect()


if __name__ == "__main__":
    configure_logging(CONFIG)
    print_startup_banner(CONFIG, __version__)
    LOGGER.info(
        "Logger initialized at level %s; configuration loaded from %s.",
        CONFIG.logging.level.upper(),
        CONFIG.config_path,
    )
    LOGGER.info("Runtime profile: %s (standalone API).", CONFIG.app.env)
    try:
        asyncio.run(preflight_startup())
    except RuntimeError as exc:
        LOGGER.error(
            str(exc),
            extra={
                "operation": LogOperation.STARTUP,
                "resource": "application",
                "outcome": LogOutcome.FAILED,
            },
        )
        raise SystemExit(1) from None
    LOGGER.info(
        "Startup checks completed; binding HTTP server to %s:%s.",
        CONFIG.app.host,
        CONFIG.app.port,
        extra={
            "operation": LogOperation.STARTUP,
            "resource": "http_server",
            "outcome": LogOutcome.SUCCESS,
        },
    )
    uvicorn.run(
        app,
        host=CONFIG.app.host,
        port=CONFIG.app.port,
        reload=False,
        log_config=None,
        access_log=False,
    )

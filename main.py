"""Cloudrave backend entrypoint."""

from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn

from app.dep import Dep
from app.factory import create_app
from config import load_config
from logging_config import configure_logging, get_logger


ROOT_DIR = Path(__file__).resolve().parent
CONFIG = load_config(ROOT_DIR)
app = create_app(CONFIG)
LOGGER = get_logger("cloudrave.main")


async def preflight_startup() -> None:
    dep = Dep(CONFIG)
    try:
        await dep.connect()
    finally:
        await dep.disconnect()


if __name__ == "__main__":
    configure_logging(CONFIG)
    try:
        asyncio.run(preflight_startup())
    except RuntimeError as exc:
        LOGGER.error(
            str(exc),
            extra={"request_id": "bootstrap", "ip": "-", "actor": "system"},
        )
        raise SystemExit(1) from None
    uvicorn.run(
        "main:app",
        host=CONFIG.app.host,
        port=CONFIG.app.port,
        reload=False,
    )

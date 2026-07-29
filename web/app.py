"""FastAPI application factory."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from core.ver import __version__
from api.v1 import router as v1_router
from core.dep import Dep
from web.err import install_exception_handlers
from web.mid.log import LoggingMiddleware
from web.mid.rid import RequestIdMiddleware
from web.mid.sec import SecurityHeadersMiddleware
from web.mid.spa import SpaFallbackMiddleware
from core.cfg import CloudraveConfig
from core.log import LogOperation, LogOutcome, configure_logging, get_logger


LOGGER = get_logger("cloudrave.lifecycle")
ASSETS_DIR_NAME = "assets"


def _is_pyinstaller_bundle() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _resolve_assets_dir(config: CloudraveConfig) -> Path | None:
    """Locate bundled frontend assets.

    When running from source, assets live in the repository root. When
    packaged with PyInstaller, assets are bundled next to the executable.
    """
    candidates: list[Path] = []

    if _is_pyinstaller_bundle():
        meipass = Path(sys._MEIPASS).resolve()  # type: ignore[attr-defined]
        candidates.append(meipass / ASSETS_DIR_NAME)
        candidates.append(Path(sys.executable).resolve().parent / ASSETS_DIR_NAME)

    candidates.append(config.root_dir / ASSETS_DIR_NAME)

    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    dep: Dep = app.state.dep
    LOGGER.info("Opening application services.")
    try:
        await dep.connect()
    except Exception:
        LOGGER.exception(
            "Application services failed to start.",
            extra={
                "operation": LogOperation.STARTUP,
                "resource": "application",
                "outcome": LogOutcome.FAILED,
            },
        )
        await dep.disconnect()
        raise

    LOGGER.info(
        "Application services are ready.",
        extra={
            "operation": LogOperation.STARTUP,
            "resource": "application",
            "outcome": LogOutcome.SUCCESS,
        },
    )
    try:
        yield
    finally:
        LOGGER.info("Closing application services.")
        await dep.disconnect()
        LOGGER.info(
            "Application services stopped cleanly.",
            extra={
                "operation": LogOperation.SHUTDOWN,
                "resource": "application",
                "outcome": LogOutcome.SUCCESS,
            },
        )


def create_app(config: CloudraveConfig) -> FastAPI:
    configure_logging(config)
    config.local_upload_root_path.mkdir(parents=True, exist_ok=True)
    config.preview_cache_root_path.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title=config.app.name,
        version=__version__,
        docs_url=None if config.app.env == "production" else "/api/docs",
        redoc_url=None if config.app.env == "production" else "/api/redoc",
        lifespan=lifespan,
    )

    dep = Dep(config)
    app.state.dep = dep

    # Added from inner to outer. Authentication endpoints retain their focused
    # anti-brute-force dependency; ordinary API traffic is not globally capped.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.app.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "X-Device-Fingerprint",
            "X-Cloudrave-Local-Write",
            "X-Cloudrave-Local-Part",
            "If-Match",
            "If-None-Match",
            "If-Range",
            "Range",
        ],
        expose_headers=["Accept-Ranges", "Content-Length", "Content-Range", "ETag", "Last-Modified"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware, header_name=config.app.request_id_header)

    install_exception_handlers(app)

    app.include_router(v1_router, prefix="/api/v1")

    assets_dir = _resolve_assets_dir(config)
    if assets_dir is not None:
        app.mount(
            "/",
            StaticFiles(directory=assets_dir, html=True, check_dir=False),
            name="static",
        )
        app.add_middleware(
            SpaFallbackMiddleware,
            index_html=assets_dir / "index.html",
        )

        app.state.spa_mode = True
        app.state.assets_dir = assets_dir
        LOGGER.info(
            "Serving embedded frontend from %s (fullstack mode).",
            assets_dir,
            extra={
                "operation": LogOperation.STARTUP,
                "resource": "static_assets",
                "outcome": LogOutcome.SUCCESS,
            },
        )
    else:
        app.state.spa_mode = False
        app.state.assets_dir = None

    return app

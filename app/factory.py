"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1 import router as v1_router
from app.dep import Dep
from app.exception_handlers import install_exception_handlers
from app.middleware.logging import LoggingMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from config import CloudraveConfig
from logging_config import LogOperation, LogOutcome, configure_logging, get_logger


LOGGER = get_logger("cloudrave.lifecycle")


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

    app = FastAPI(
        title=config.app.name,
        version=__version__,
        docs_url=None if config.app.env == "production" else "/api/docs",
        redoc_url=None if config.app.env == "production" else "/api/redoc",
        lifespan=lifespan,
    )

    dep = Dep(config)
    app.state.dep = dep

    # Added from inner to outer. Keep request ids and access logging around every
    # response, including middleware short-circuits such as rate limiting.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.app.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "X-Cloudrave-Local-Write",
            "X-Cloudrave-Local-Part",
        ],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware, header_name=config.app.request_id_header)

    install_exception_handlers(app)

    app.include_router(v1_router, prefix="/api/v1")
    return app

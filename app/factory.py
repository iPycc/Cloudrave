"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import router as v1_router
from app.dep import Dep
from app.middleware.logging import LoggingMiddleware
from app.middleware.request_id import RequestIdMiddleware
from config import CloudraveConfig
from logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    dep: Dep = app.state.dep
    await dep.connect()
    yield
    await dep.disconnect()


def create_app(config: CloudraveConfig) -> FastAPI:
    configure_logging(config)
    config.local_upload_root_path.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title=config.app.name,
        version="0.1.0",
        docs_url="/api/docs",
        lifespan=lifespan,
    )

    dep = Dep(config)
    app.state.dep = dep

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.app.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(v1_router, prefix="/api/v1")
    return app

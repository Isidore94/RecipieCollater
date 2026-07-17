"""FastAPI application factory, lifespan, middleware, and error handlers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.auth import (
    AuthRequired,
    CSRFError,
    IngestAuthError,
    auth_required_handler,
    csrf_handler,
    ingest_auth_handler,
)
from app.config import PACKAGE_DIR, get_settings
from app.db import run_migrations
from app.logging_config import configure_logging, get_logger
from app.routers import admin, health, onboarding, pages

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    # Idempotent: brings a fresh install up to the current schema and is a no-op when a
    # staged update already migrated the copy. Snapshots are taken before each apply.
    result = run_migrations(settings.db_path, backup_dir=settings.backups_dir / "pre_migration")
    log.info(
        "startup",
        version=__version__,
        db=str(settings.db_path),
        schema_version=result.current_version,
        applied=result.applied,
    )
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(console=settings.log_console)

    app = FastAPI(
        title="RecipeCollater",
        version=__version__,
        docs_url=None,  # no interactive API docs on the LAN app surface
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id", uuid.uuid4().hex[:12])
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=request.url.path
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["x-request-id"] = request_id
        return response

    app.add_exception_handler(AuthRequired, auth_required_handler)
    app.add_exception_handler(IngestAuthError, ingest_auth_handler)
    app.add_exception_handler(CSRFError, csrf_handler)

    app.mount(
        "/static",
        StaticFiles(directory=str(PACKAGE_DIR / "static")),
        name="static",
    )

    app.include_router(health.router)
    app.include_router(onboarding.router)
    app.include_router(admin.router)
    app.include_router(pages.router)

    return app


app = create_app()

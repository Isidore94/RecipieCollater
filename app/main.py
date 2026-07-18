"""FastAPI application factory, lifespan, middleware, and error handlers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
from app.db import connect, run_migrations
from app.logging_config import configure_logging, get_logger
from app.routers import (
    admin,
    auth,
    cooking,
    foods,
    health,
    ingest_api,
    onboarding,
    pages,
    pantry,
    receipts,
    recipes,
    shopping,
    shortcut,
)
from app.services.quantity import QuantityError
from app.services.units import seed_core_units

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    # Idempotent: brings a fresh install up to the current schema and is a no-op when a
    # staged update already migrated the copy. Snapshots are taken before each apply.
    result = run_migrations(settings.db_path, backup_dir=settings.backups_dir / "pre_migration")
    seed_conn = connect(settings.db_path)
    try:
        units_seeded = seed_core_units(seed_conn)
    finally:
        seed_conn.close()
    log.info(
        "startup",
        version=__version__,
        release_id=settings.release_id,
        db=str(settings.db_path),
        schema_version=result.current_version,
        applied=result.applied,
        units_seeded=units_seeded,
    )
    yield
    log.info("shutdown")


async def _quantity_error_handler(request: Request, exc: Exception) -> Response:
    return PlainTextResponse("That amount could not be read as a number.", status_code=400)


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

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

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
    # A bad amount string (e.g. a non-numeric ?servings=) is a client error, not a 500.
    app.add_exception_handler(QuantityError, _quantity_error_handler)

    app.mount(
        "/static",
        StaticFiles(directory=str(PACKAGE_DIR / "static")),
        name="static",
    )

    app.include_router(health.router)
    app.include_router(ingest_api.router)
    app.include_router(onboarding.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(shortcut.router)
    app.include_router(recipes.router)
    app.include_router(cooking.router)
    app.include_router(pantry.router)
    app.include_router(shopping.router)
    app.include_router(foods.router)
    app.include_router(receipts.router)
    app.include_router(pages.router)

    return app


app = create_app()

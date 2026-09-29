"""FastAPI application. The API dispatches jobs and serves state; it never runs investigations."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from datawarden.api.routes import admin, auth, catalog, events, incidents
from datawarden.config import REPO_ROOT, get_settings
from datawarden.logs import configure_logging
from datawarden.observability import setup_tracing

log = logging.getLogger("datawarden.api")
MAX_BODY_BYTES = 1_000_000


class RequestContext(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        request.state.request_id = rid if 8 <= len(rid) <= 64 and rid.replace("-", "").isalnum() else uuid.uuid4().hex
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request body too large", "request_id": request.state.request_id}, 413)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app() -> FastAPI:
    configure_logging()
    s = get_settings()
    app = FastAPI(
        title="DataWarden API",
        version="1.0.0",
        description="Data quality incident investigation and verified recovery (synthetic demo).",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.add_middleware(RequestContext)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "Last-Event-ID", "X-Request-ID"],
    )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            {"error": exc.detail, "request_id": getattr(request.state, "request_id", None)},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()[:5]]
        return JSONResponse(
            {"error": "invalid request", "details": errors, "request_id": getattr(request.state, "request_id", None)},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error", extra={"request_id": getattr(request.state, "request_id", None)})
        return JSONResponse(
            {"error": "internal error", "request_id": getattr(request.state, "request_id", None)}, status_code=500
        )

    for r in (auth.router, catalog.router, events.router, incidents.router, admin.router, admin.health):
        app.include_router(r, prefix="/api/v1")
    app.include_router(admin.health)

    dist = REPO_ROOT / "apps" / "web" / "dist"
    if dist.exists():
        app.mount("/static", StaticFiles(directory=dist / "static"), name="static")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404, "not found")
            return FileResponse(dist / "index.html")

    setup_tracing(app)
    return app


app = create_app()

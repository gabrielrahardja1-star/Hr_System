"""FastAPI application factory.

Phase 1 mounts the ingest API only. The web UI (Phase 2) mounts onto the same
app at `/`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from server import __version__
from server.api.ingest import router as ingest_router
from server.db import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="MergeCoal Attendance → Hours",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(ingest_router)

    try:
        from fastapi.staticfiles import StaticFiles

        from server.web.routes import STATIC_DIR
        from server.web.routes import router as web_router

        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        app.include_router(web_router)
    except ModuleNotFoundError:
        # Web layer arrives in Phase 2.
        pass

    return app


app = create_app()

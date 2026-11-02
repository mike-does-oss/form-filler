"""FastAPI application: API + the built React UI as static files."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api.routes import router
from app.config import REPO_ROOT, get_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# The desktop app ships the built UI next to the frozen backend and points here at it.
FRONTEND_DIST = Path(os.environ.get("FF_FRONTEND_DIST") or REPO_ROOT / "frontend" / "dist")


def create_app(start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.get_conn()  # run migrations
        worker = None
        if start_worker:
            from app.jobs.worker import Worker

            worker = Worker()
            worker.start()
        yield
        if worker:
            worker.stop()
        from app.setup_ollama import stop_started

        stop_started()  # an Ollama we started goes away with us, so the model's memory is freed

    app = FastAPI(title="Local Form Filler", version="0.1.0", lifespan=lifespan)
    app.include_router(router)

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            f = FRONTEND_DIST / path
            if path and f.is_file():
                return FileResponse(f)
            return FileResponse(FRONTEND_DIST / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def no_ui():
            return JSONResponse(
                {"detail": "UI not built. Run `npm install && npm run build` in frontend/, or use the API at /docs."}
            )

    _ = get_config, Path
    return app


app = create_app()

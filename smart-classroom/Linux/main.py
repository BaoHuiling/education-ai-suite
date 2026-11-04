import sys
import warnings
warnings.filterwarnings("ignore", message=r"[\s\S]*torchcodec is not installed correctly")

from utils import system_checker
from model_manager.feature_bootstrap import (
    startup,
    resolve_effective_features,
    NO_FEATURES_MESSAGE,
)

from utils.logger_config import setup_logger
setup_logger()

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from api.endpoints import register_routes
from model_manager.capability.runner import QueueFullError, OomError
from utils.runtime_config_loader import RuntimeConfig
from utils.ensure_model import ensure_model
import logging
from fastapi.middleware.cors import CORSMiddleware
import os
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from pathlib import Path
from contextlib import asynccontextmanager


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup(app)
    # Mount the SPA fallback AFTER feature routers are registered by startup(),
    # so the catch-all is last in the route table and its reserved-segment set
    # includes every feature API route (video_analytics, ...). Registering it at
    # import time shadowed those GET routes, since feature routers are only
    # added here in the lifespan.
    _mount_spa(app)
    from utils.session_store import SessionStore
    SessionStore.recover_after_restart()
    yield
    # Shutdown: drain in-flight capability work and release device (GPU) memory.
    from api.proxy import close_proxy_client
    await close_proxy_client(app)
    from model_manager import ModelManager
    logger.info("Shutdown: draining capabilities and releasing devices...")
    ModelManager.instance().shutdown()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # For Testing ["*"]
    allow_credentials=True,          # cookies/auth allowed
    allow_methods=["*"],             # allow all HTTP methods
    allow_headers=["*"],             # allow all headers
    expose_headers=["x-session-id"]  # expose custom headers if needed
)

register_routes(app)

# Same-origin passthrough to the no-CORS backend (content_search :9011).
# Registered after register_routes() so nothing is shadowed and
# before _mount_spa() so the SPA catch-all reserves these prefixes.
from api.proxy import register_proxy_routes, register_realtime_ws_proxy
register_proxy_routes(app)
# WebSocket passthrough so the browser can stream live-mic audio to the
# audio-analyzer's realtime transcription endpoint over the app origin.
register_realtime_ws_proxy(app)


def _mount_spa(app: FastAPI) -> None:
    """Serve the built React SPA (``ui/dist``) from the FastAPI app.

    Mounts the hashed Vite assets under ``/assets`` and adds an SPA-fallback
    GET route that returns ``index.html`` for any non-API path (deep links /
    refresh).

    Registered *after* ``register_routes(app)`` so API routes always win. The
    set of reserved top-level path segments is derived from the routes already
    registered, so the fallback never shadows an API route nor masks a genuine
    API 404 (those stay JSON, not HTML). Skipped with a warning when the build
    is absent (dev serves the UI via the Vite dev server instead).
    """
    ui_dist = (Path(__file__).resolve().parent / "ui" / "dist")
    index_file = ui_dist / "index.html"
    if not index_file.is_file():
        logger.warning(
            "UI build not found at %s; skipping SPA mount "
            "(dev serves the UI via the Vite dev server).",
            ui_dist,
        )
        return

    assets_dir = ui_dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # Anything already claimed by an API route (its first path segment) stays
    # off-limits to the SPA fallback. Reverse-proxy prefixes (/api/v1/object,
    # ...) are folded in explicitly so their passthrough routes
    # win over the index.html fallback.
    from api.proxy import PROXY_RESERVED_SEGMENTS
    reserved = set(PROXY_RESERVED_SEGMENTS)
    for route in app.routes:
        path = getattr(route, "path", "") or ""
        segment = path.lstrip("/").split("/", 1)[0]
        if segment:
            reserved.add(segment)

    dist_root = ui_dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        top = full_path.split("/", 1)[0]
        if top in reserved:
            # A genuine API miss must 404 as JSON, not get swallowed into HTML.
            raise HTTPException(status_code=404, detail="Not Found")
        # Serve a real build file when it exists (favicon, manifest, robots…),
        # otherwise fall back to index.html for client-side routes / refresh.
        candidate = (dist_root / full_path).resolve()
        if candidate.is_file() and str(candidate).startswith(str(dist_root)):
            return FileResponse(str(candidate))
        return FileResponse(str(index_file))


# NOTE: _mount_spa(app) is invoked from the lifespan startup (after feature
# routers are registered), not here at import time — otherwise the SPA catch-all
# would precede and shadow feature GET routes.


@app.exception_handler(QueueFullError)
async def _queue_full_handler(request: Request, exc: QueueFullError):
    """Map QueueFullError to HTTP 503 with a Retry-After hint."""
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "2"},
        content={"detail": str(exc), "retryAfterSeconds": 2},
    )


@app.exception_handler(OomError)
async def _oom_handler(request: Request, exc: OomError):
    """Map OomError (GPU/CPU memory pressure) to HTTP 503 with a Retry-After hint."""
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "5"},
        content={"detail": str(exc), "retryAfterSeconds": 5, "reason": "memory_pressure"},
    )


def system_check():
    if (not system_checker.check_system_requirements()) and (not system_checker.show_warning_and_prompt_user_to_continue()):
        sys.exit(1)

if __name__ == "__main__":
    system_check()
    RuntimeConfig.ensure_config_exists()

    if not resolve_effective_features().features:
        logger.error("%s. Exiting.", NO_FEATURES_MESSAGE)
        sys.exit(1)

    ensure_model()

    import uvicorn
    logger.info("App started, Starting Server...")
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        timeout_graceful_shutdown=5,
    )

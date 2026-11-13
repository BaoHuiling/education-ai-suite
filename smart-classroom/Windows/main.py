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

from fastapi import FastAPI, Request
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
    from utils.session_store import SessionStore
    SessionStore.recover_after_restart()
    yield
    # Shutdown: record stages still running (VA on its daemon thread) as
    # interrupted before anything else can stall the exit. Usually a no-op by
    # now: _Server.handle_exit already did it when the stop signal arrived.
    from utils.stage_tracker import close_open_stages
    close_open_stages()
    # Then drain in-flight capability work and release device (GPU) memory.
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
    
    RuntimeConfig.ensure_config_exists()

    if not resolve_effective_features().features:
        logger.error("%s. Exiting.", NO_FEATURES_MESSAGE)
        sys.exit(1)

    ensure_model()

    import threading
    import uvicorn
    from utils.stage_tracker import close_open_stages

    class _Server(uvicorn.Server):
        """Records running stages as interrupted the moment a stop signal arrives.

        The lifespan shutdown runs only after open connections close (up to
        timeout_graceful_shutdown), a second Ctrl+C skips it, and whatever
        launched the backend may kill it before then. VA, which runs as long
        as its videos, would be left with no record. On a thread: a signal
        handler interrupts the main thread, which may hold the locks
        close_open_stages() takes.
        """

        def handle_exit(self, sig, frame):
            if not self.should_exit:
                threading.Thread(target=close_open_stages, name="close-open-stages",
                                 daemon=True).start()
            super().handle_exit(sig, frame)

    logger.info("App started, Starting Server...")
    server = _Server(uvicorn.Config(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        timeout_graceful_shutdown=5,
    ))
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # as uvicorn.run(): the captured Ctrl+C is re-raised after shutdown
    if not server.started:
        sys.exit(3)  # uvicorn's STARTUP_FAILURE

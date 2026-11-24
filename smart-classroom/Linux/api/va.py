"""Video Analytics HTTP API (session-aware).

Thin FastAPI surface over ``components.va.va_service``; the heavy lifting runs in
the ``video-analytics`` side-car. This layer maps sessions, validates requests,
builds browser stream URLs, and proxies the side-car's NDJSON streams.
"""
import json
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse

from components.va.schemas import VideoAnalyticsRequest, VideoAnalyticsStopRequest
from components.va.va_client import VALID_PIPELINE_NAMES
from components.va.va_service import get_service
from utils.video_util import save_video_file

logger = logging.getLogger(__name__)

va_router = APIRouter()

# Session ids become filesystem path segments — constrain against traversal.
_PATH_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _require_session(x_session_id: Optional[str]) -> str:
    if not x_session_id:
        raise HTTPException(status_code=400, detail="Missing required header: x-session-id")
    if not _PATH_SAFE_SESSION_ID.match(x_session_id):
        raise HTTPException(status_code=400, detail="Invalid x-session-id")
    return x_session_id


@va_router.post("/upload-video")
def upload_video(
    file: UploadFile = File(...),
    pipeline_name: str = Form(...),
    x_session_id: Optional[str] = Header(None),
):
    """Save an uploaded clip into the shared storage volume and return its
    container path, to pass back as the pipeline ``source``."""
    session_id = _require_session(x_session_id)
    if pipeline_name not in VALID_PIPELINE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid pipeline_name '{pipeline_name}'. "
            f"Must be one of: {list(VALID_PIPELINE_NAMES)}",
        )
    try:
        filename, filepath = save_video_file(file, session_id, pipeline_name)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Error saving uploaded video: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "filename": filename,
            "pipeline_name": pipeline_name,
            "message": "Video uploaded successfully",
            "path": filepath,
        },
    )


@va_router.post("/start-video-analytics-pipeline")
def start_video_analytics_pipeline(
    requests: List[VideoAnalyticsRequest],
    request: Request,
    x_session_id: Optional[str] = Header(None),
):
    """Start one or more video analytics pipelines for a session.

    Body: list of {pipeline_name, source, output_stream}. Returns per-pipeline
    results with HLS/WebRTC ``stream_url`` + ``stream_protocol`` when
    ``output_stream`` is true. A loopback host in the configured stream base
    is replaced by the host this request was addressed to, so a browser on
    another machine gets a URL that points at this server.
    """
    session_id = _require_session(x_session_id)
    if not requests:
        raise HTTPException(status_code=400, detail="Request array cannot be empty")

    for req in requests:
        if req.pipeline_name not in VALID_PIPELINE_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid pipeline_name '{req.pipeline_name}'. "
                f"Must be one of: {list(VALID_PIPELINE_NAMES)}",
            )
        if req.source is None or req.source.strip() == "":
            raise HTTPException(
                status_code=400,
                detail=f"Source cannot be empty for pipeline '{req.pipeline_name}'",
            )

    try:
        results = get_service().start_pipelines(
            session_id, requests, client_host=request.url.hostname
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Error starting video analytics pipelines: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content={"results": results}, status_code=200)


@va_router.post("/stop-video-analytics-pipeline")
def stop_video_analytics_pipeline(
    requests: List[VideoAnalyticsStopRequest],
    x_session_id: Optional[str] = Header(None),
):
    """Stop one or more video analytics pipelines for a session."""
    session_id = _require_session(x_session_id)
    if not requests:
        raise HTTPException(status_code=400, detail="Request array cannot be empty")

    for req in requests:
        if req.pipeline_name not in VALID_PIPELINE_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid pipeline_name '{req.pipeline_name}'. "
                f"Must be one of: {list(VALID_PIPELINE_NAMES)}",
            )

    try:
        results = get_service().stop_pipelines(session_id, requests)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error stopping video analytics pipelines: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return JSONResponse(content={"results": results}, status_code=200)


@va_router.get("/monitor-video-analytics-pipeline")
def monitor_video_analytics_pipeline_status(
    x_session_id: Optional[str] = Header(None),
):
    """Stream combined pipeline status as newline-delimited JSON."""
    _require_session(x_session_id)
    service = get_service()

    def stream():
        try:
            for status in service.monitor_stream():
                yield json.dumps(status) + "\n"
        except Exception as exc:  # noqa: BLE001
            logger.error("Error streaming VA status: %s", exc)
            yield json.dumps({"error": str(exc)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@va_router.get("/class-statistics")
def get_class_statistics(x_session_id: Optional[str] = Header(None)):
    """Stream classroom statistics as newline-delimited JSON.

    Each line: {student_count, stand_count, raise_up_count, stand_reid[]}.
    """
    session_id = _require_session(x_session_id)
    service = get_service()

    def stream():
        try:
            for stats in service.class_statistics_stream(session_id):
                yield json.dumps(stats) + "\n"
        except Exception as exc:  # noqa: BLE001
            logger.error("Error streaming class statistics: %s", exc)
            yield json.dumps({"error": str(exc)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")

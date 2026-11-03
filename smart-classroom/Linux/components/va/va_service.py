"""Session-aware Video Analytics orchestration + statistics aggregation.

Application-owned layer between ``api/va.py`` and the stateless
``video-analytics`` side-car (via ``components.va.va_client``): maps a session to
its shared VA output dir, launches/stops per-camera pipelines, builds browser
stream URLs, aggregates ``class_statistics.json`` on completion, and proxies the
side-car's status / class-statistics NDJSON streams.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlparse

from components.va.schemas import VideoAnalyticsRequest, VideoAnalyticsStopRequest
from components.va.va_client import VALID_PIPELINE_NAMES, VideoAnalyticsClient, get_client
from utils.config_loader import config
from utils.session_paths import SessionPaths
from utils.session_state_manager import SessionState

logger = logging.getLogger(__name__)

# VA spans two endpoints (start/stop), so it cannot use the stage_tracker
# context manager; the stage is written directly, best-effort.
_VA_STAGE = "video_analytics"


def _set_va_stage(session_id: str, status: str, error: Optional[str] = None) -> None:
    """Best-effort VA stage update (no-op if the session isn't workflow-tracked)."""
    try:
        from utils.session_store import SessionStore

        SessionStore.set_stage(session_id, _VA_STAGE, status)
    except Exception as exc:  # noqa: BLE001 - stage tracking is best-effort
        logger.debug("[VA] set_stage(%s=%s) skipped: %s", session_id, status, exc)
    if error:
        logger.warning("[VA] stage %s -> %s: %s", session_id, status, error)


def _va_device() -> Optional[str]:
    va = getattr(getattr(config, "models", None), "va", None)
    return getattr(va, "device", None) if va is not None else None


def _va_threshold() -> float:
    va = getattr(getattr(config, "models", None), "va", None)
    try:
        return float(getattr(va, "threshold", 0.5))
    except (TypeError, ValueError):
        return 0.5


def _va_pipeline_cfg(attr: str, default):
    vp = getattr(config, "va_pipeline", None)
    return getattr(vp, attr, default) if vp is not None else default


def _output_rtsp_url() -> str:
    return str(_va_pipeline_cfg("output_rtsp_url", "rtsp://mediamtx:8554"))


def _stream_protocol() -> str:
    return str(_va_pipeline_cfg("stream_protocol", "webrtc"))


def _stream_base_url(protocol: str) -> str:
    if protocol == "webrtc":
        return str(_va_pipeline_cfg("webrtc_base_url", "http://localhost:8889"))
    return str(_va_pipeline_cfg("hls_base_url", "http://localhost:8888"))


class _SessionVA:
    """Per-session VA bookkeeping."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.launched: set[str] = set()
        self.watcher: Optional[threading.Thread] = None
        self.final_status: Dict[str, Optional[str]] = {}


class VideoAnalyticsService:
    """Session-aware façade over the video-analytics side-car."""

    def __init__(self, client: Optional[VideoAnalyticsClient] = None) -> None:
        self.client = client or get_client()
        self._sessions: Dict[str, _SessionVA] = {}
        self._sessions_lock = threading.Lock()

    # ── session bookkeeping ───────────────────────────────────────────────
    def _session(self, session_id: str) -> _SessionVA:
        with self._sessions_lock:
            sv = self._sessions.get(session_id)
            if sv is None:
                sv = _SessionVA()
                self._sessions[session_id] = sv
            return sv

    @staticmethod
    def _va_dir(session_id: str) -> str:
        """Absolute VA output dir, shared with the side-car at the same path."""
        return str(SessionPaths.va_dir(session_id).resolve())

    @staticmethod
    def _front_posture_file(session_id: str) -> str:
        return os.path.join(VideoAnalyticsService._va_dir(session_id), "front_posture.txt")

    # Characters that could break out of the side-car's gst-parse pipeline
    # description (quote/backslash close a value, '!' links a new element) or
    # smuggle control bytes. Rejected in any accepted source.
    _GST_UNSAFE = set('"\\!')

    @staticmethod
    def _validate_source(session_id: str, source: str) -> Optional[str]:
        """Allow only a well-formed rtsp(s)/rtspt URL or a file inside this
        session's VA dir (an uploaded clip), so the side-car can neither be aimed
        at an arbitrary file nor have GStreamer elements injected via the source."""
        if not source or not source.strip():
            return "source is empty"
        src = source.strip()

        # Reject anything that could break the side-car's gst-parse string
        # (defense-in-depth: the source is interpolated into a pipeline
        # description). Covers both the rtsp and file branches.
        if any(c in VideoAnalyticsService._GST_UNSAFE for c in src) or any(
            ord(c) < 0x20 for c in src
        ):
            return "source contains invalid characters"

        low = src.lower()
        if low.startswith(("rtsp://", "rtsps://", "rtspt://")):
            if any(c.isspace() for c in src):
                return "rtsp source must not contain whitespace"
            parsed = urlparse(src)
            if not parsed.scheme or not parsed.netloc:
                return "malformed rtsp url"
            return None
        try:
            resolved = os.path.realpath(src)
            va_root = os.path.realpath(VideoAnalyticsService._va_dir(session_id))
            if resolved == va_root or resolved.startswith(va_root + os.sep):
                return None
        except Exception:  # noqa: BLE001
            pass
        return (
            "source must be an rtsp:// URL or a file uploaded for this session "
            "via /upload-video"
        )

    # ── health ────────────────────────────────────────────────────────────
    def health(self) -> bool:
        return self.client.health()

    # ── launch ────────────────────────────────────────────────────────────
    def start_pipelines(
        self, session_id: str, requests: List[VideoAnalyticsRequest]
    ) -> List[Dict]:
        sv = self._session(session_id)
        output_dir = self._va_dir(session_id)
        os.makedirs(output_dir, exist_ok=True)

        device = _va_device()
        threshold = _va_threshold()
        output_rtsp = _output_rtsp_url()
        protocol = _stream_protocol()
        stream_base = _stream_base_url(protocol)

        results: List[Dict] = []
        with sv.lock:
            newly_opened_stage = len(sv.launched) == 0
            if newly_opened_stage:
                _set_va_stage(session_id, "running")

            for req in requests:
                result = {
                    "status": "success",
                    "pipeline_name": req.pipeline_name,
                    "session_id": session_id,
                    "output_stream": req.output_stream,
                    "device": device,
                }
                if req.pipeline_name in sv.launched:
                    results.append({
                        "status": "error",
                        "pipeline_name": req.pipeline_name,
                        "session_id": session_id,
                        "error": f"Pipeline '{req.pipeline_name}' already running",
                    })
                    continue
                src_error = self._validate_source(session_id, req.source or "")
                if src_error:
                    results.append({
                        "status": "error",
                        "pipeline_name": req.pipeline_name,
                        "session_id": session_id,
                        "error": src_error,
                    })
                    continue
                try:
                    launched = self.client.launch(
                        req.pipeline_name,
                        req.source,
                        device=device,
                        output_dir=output_dir,
                        output_rtsp=output_rtsp,
                        output_stream=req.output_stream,
                        threshold=threshold,
                        record=False,
                    )
                except Exception as exc:  # noqa: BLE001 - surface per-pipeline
                    logger.error("[VA] launch %s failed: %s", req.pipeline_name, exc)
                    results.append({
                        "status": "error",
                        "pipeline_name": req.pipeline_name,
                        "session_id": session_id,
                        "error": str(exc),
                    })
                    continue

                if not launched:
                    results.append({
                        "status": "error",
                        "pipeline_name": req.pipeline_name,
                        "session_id": session_id,
                        "error": f"Failed to start pipeline '{req.pipeline_name}'",
                    })
                    continue

                sv.launched.add(req.pipeline_name)
                if req.output_stream:
                    result["stream_url"] = f"{stream_base}/{req.pipeline_name}_stream"
                    result["stream_protocol"] = protocol
                    result["overlays_embedded"] = True
                results.append(result)

            any_ok = any(r.get("status") == "success" for r in results)
            if newly_opened_stage and not any_ok:
                _set_va_stage(
                    session_id, "failed", "no video-analytics pipeline could be launched"
                )
            elif any_ok:
                self._ensure_watcher(session_id, sv)

        return results

    # ── stop ──────────────────────────────────────────────────────────────
    def stop_pipelines(
        self, session_id: str, requests: List[VideoAnalyticsStopRequest]
    ) -> List[Dict]:
        sv = self._session(session_id)
        results: List[Dict] = []
        with sv.lock:
            for req in requests:
                try:
                    stopped = self.client.stop(req.pipeline_name)
                    sv.launched.discard(req.pipeline_name)
                    results.append({
                        "status": "success" if stopped else "error",
                        "pipeline_name": req.pipeline_name,
                        "session_id": session_id,
                        **({} if stopped else {"error": "pipeline was not running"}),
                    })
                except Exception as exc:  # noqa: BLE001
                    logger.error("[VA] stop %s failed: %s", req.pipeline_name, exc)
                    results.append({
                        "status": "error",
                        "pipeline_name": req.pipeline_name,
                        "session_id": session_id,
                        "error": str(exc),
                    })
        return results

    def stop_all(self, session_id: str, timeout: float = 5.0) -> None:
        """Stop every pipeline launched for a session (used on cancel/teardown)."""
        sv = self._session(session_id)
        with sv.lock:
            names = list(sv.launched)
        for name in names:
            try:
                self.client.stop(name, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[VA] stop_all %s/%s: %s", session_id, name, exc)
        with sv.lock:
            sv.launched.clear()

    # ── streaming proxies ─────────────────────────────────────────────────
    def monitor_stream(self, check_interval: float = 2.0) -> Iterator[Dict]:
        yield from self.client.stream_status(check_interval=check_interval)

    def class_statistics_stream(
        self, session_id: str, poll_interval: float = 5.0
    ) -> Iterator[Dict]:
        yield from self.client.stream_class_statistics(
            self._front_posture_file(session_id), poll_interval=poll_interval
        )

    # ── completion watcher + aggregation ──────────────────────────────────
    def _ensure_watcher(self, session_id: str, sv: _SessionVA) -> None:
        if sv.watcher is not None and sv.watcher.is_alive():
            return
        t = threading.Thread(
            target=self._watch_completion,
            args=(session_id,),
            name=f"va-watch-{session_id}",
            daemon=True,
        )
        sv.watcher = t
        t.start()

    def _watch_completion(self, session_id: str) -> None:
        """Consume the status stream until it ends (all pipelines settled), then
        aggregate class_statistics.json and close the VA stage."""
        sv = self._session(session_id)
        try:
            for update in self.client.stream_status(check_interval=2.0):
                for entry in update.get("pipelines", []):
                    fs = entry.get("final_status")
                    if fs is not None:
                        sv.final_status[entry["pipeline_name"]] = fs
        except Exception as exc:  # noqa: BLE001 - never kill the thread silently
            logger.error("[VA] status watcher for %s failed: %s", session_id, exc)

        statuses = list(sv.final_status.values())
        finished_ok = any(s in ("eos", "stopped") for s in statuses)
        try:
            self.aggregate_class_statistics(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("[VA] aggregation for %s failed: %s", session_id, exc)
        _set_va_stage(
            session_id,
            "done" if finished_ok or not statuses else "failed",
            None if finished_ok or not statuses
            else f"no pipeline ended normally; final statuses: {statuses}",
        )
        with sv.lock:
            sv.launched.clear()

    def aggregate_class_statistics(self, session_id: str) -> Optional[Dict]:
        """Persist class_statistics.json (cumulative pose stats + video duration)."""
        front_posture = self._front_posture_file(session_id)
        if not os.path.exists(front_posture):
            logger.info("[VA] no front_posture.txt for %s; skipping stats", session_id)
            return None

        stats = self.client.latest_class_statistics(front_posture) or {}

        video_dur = SessionState.get_video_duration(session_id)
        if isinstance(video_dur, (int, float)) and video_dur > 0:
            stats["duration_sec"] = round(float(video_dur), 1)

        stats_path = str(SessionPaths.class_statistics_path(session_id))
        os.makedirs(os.path.dirname(stats_path), exist_ok=True)
        with open(stats_path, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, indent=2, ensure_ascii=False)
        logger.info("[VA] saved class_statistics.json for %s: %s", session_id, stats)
        return stats


# Process-wide singleton (one side-car, one aggregation owner).
_service: Optional[VideoAnalyticsService] = None
_service_lock = threading.Lock()


def get_service() -> VideoAnalyticsService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = VideoAnalyticsService()
    return _service

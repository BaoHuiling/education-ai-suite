"""kiosk-ui-server — serves the React single-page app and proxies backends.

This lightweight FastAPI app replaces the previous Gradio UI. It:

1. Serves the built React app (``kiosk-ui-react/dist``) as static files.
2. Reverse-proxies API calls to the backend services so the browser talks to a
   single origin (no CORS juggling):

   - ``/api/kiosk/*``    -> kiosk-core       (default http://127.0.0.1:8012)
   - ``/api/rag/*``      -> rag-service      (default http://127.0.0.1:8020)
   - ``/api/tts/*``      -> text-to-speech   (default http://127.0.0.1:8011)
   - ``/api/analyzer/*`` -> audio-analyzer   (default http://127.0.0.1:8010)

Run: python ata_ui_server.py   (listens on 0.0.0.0:7860)
"""
from __future__ import annotations

import os
import ipaddress
import time
import json
import tempfile
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "assistant-react-ui" / "dist"

UPSTREAMS = {
    "kiosk": os.getenv("KIOSK_UI_KIOSK_CORE_URL", "http://127.0.0.1:8012"),
    "rag": os.getenv("KIOSK_UI_RAG_URL", "http://127.0.0.1:8020"),
    "tts": os.getenv("KIOSK_UI_TTS_URL", "http://127.0.0.1:8011"),
    "analyzer": os.getenv("KIOSK_UI_ANALYZER_URL", "http://127.0.0.1:8010"),
}

_VOICE_ACTIVATION_ROOT = r"SOFTWARE\Microsoft\Speech_OneCore\Settings\VoiceActivation"
_CORTANA_SIGNAL_PATH = r"{787E8510-069C-456F-AECE-068D884999EE}\1033\{86FA368E-357C-408C-BFCF-B89CBA2B0006}"
_hardware_wake_event: dict[str, object] | None = None
# Last time a browser polled wake status; a recent poll means an open UI can record the question.
_last_ui_status_poll = 0.0
_UI_ALIVE_SECONDS = 3.0

# Long timeout: RAG generation + TTS synthesis can take a while.
_PROXY_TIMEOUT = float(os.getenv("KIOSK_UI_PROXY_TIMEOUT_SECONDS", "600"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pooled client shared by every proxied request. Creating a client per
    # request rebuilds the connection pool each call and collapses throughput
    # under the browser's concurrent polling; a shared pool keeps it fast.
    app.state.client = httpx.AsyncClient(
        timeout=_PROXY_TIMEOUT,
        trust_env=False,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
    )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="kiosk-ui-server", lifespan=lifespan)

# Hop-by-hop headers must not be forwarded.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def _filter_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _is_hardware_wake_armed() -> bool:
    if os.name != "nt":
        return False
    app_aumid = os.getenv("ATA_WAKEWORD_AGENT_AUMID", "").strip()
    if not app_aumid:
        return False
    try:
        import winreg

        app_path = rf"{_VOICE_ACTIVATION_ROOT}\{app_aumid}"
        config_path = rf"{_VOICE_ACTIVATION_ROOT}\SignalDetectionConfigurations\{app_aumid}\{_CORTANA_SIGNAL_PATH}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, app_path) as key:
            app_enabled = winreg.QueryValueEx(key, "AgentActivationEnabled")[0] == 1
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, config_path) as key:
            available = winreg.QueryValueEx(key, "Available")[0] == 1
            active = winreg.QueryValueEx(key, "Active")[0] == 1
        return app_enabled and available and active
    except (OSError, ValueError):
        return False


def _recover_wake_event() -> dict[str, object] | None:
    event = None
    try:
        log_path = Path(tempfile.gettempdir()) / "AtaWakeAgent.log"
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 65536))
            lines = log.read().decode("utf-8", errors="replace").splitlines()
        for line in lines:
            timestamp, _, message = line.partition(" ")
            try:
                detected_at = datetime.fromisoformat(timestamp).timestamp()
            except ValueError:
                continue
            if "Worker started. Arguments:" in message and "--wake" in message:
                event = {"detected_at": detected_at, "session_id": None}
            elif event is not None and "[AtaBridge] POST " in message and "; response: " in message:
                try:
                    payload = json.loads(message.partition("; response: ")[2])
                    if isinstance(payload.get("session_id"), str):
                        event["session_id"] = payload["session_id"]
                except (ValueError, AttributeError):
                    pass
            elif event is not None and "ATA trigger failed." in message:
                event["error"] = "Wake activation received, but ATA could not start a microphone session."
    except OSError:
        pass
    return event


@app.get("/api/wakeword/status")
async def wakeword_status(request: Request) -> dict[str, object]:
    global _hardware_wake_event, _last_ui_status_poll
    _last_ui_status_poll = time.time()
    backend = os.getenv("ATA_WAKEWORD_BACKEND", "software").strip().lower()
    if backend != "hardware":
        return {"backend": backend, "state": "disabled", "session_id": None}
    if not _is_hardware_wake_armed():
        return {"backend": backend, "state": "not_armed", "session_id": None}

    recovered = _recover_wake_event()
    if recovered is not None and (
        _hardware_wake_event is None
        or float(recovered["detected_at"]) > float(_hardware_wake_event["detected_at"])
    ):
        _hardware_wake_event = recovered
    event = _hardware_wake_event
    if event is not None:
        session_id = event.get("session_id")
        detected_at = float(event["detected_at"])
        details = {"detected_at": detected_at, "session_id": session_id, "capture": event.get("capture")}
        if event.get("error"):
            return {"backend": backend, "state": "failed", "error": event["error"], **details}
        if session_id is not None:
            try:
                client: httpx.AsyncClient = request.app.state.client
                response = await client.get(f"{UPSTREAMS['kiosk']}/api/v1/sessions/{session_id}", timeout=2)
                if response.status_code == 200:
                    snapshot = response.json()
                    session_status = snapshot.get("status")
                    if session_status == "failed":
                        return {"backend": backend, "state": "failed", "error": snapshot.get("error"), **details}
                    if session_status in {"running", "stopping"}:
                        return {"backend": backend, "state": "detected", **details}
            except httpx.RequestError:
                pass

        hold_seconds = 10 if session_id is None else 5
        if time.time() - detected_at < hold_seconds:
            return {"backend": backend, "state": "detected", **details}
        return {"backend": backend, "state": "waiting", **details}

    return {"backend": backend, "state": "waiting", "session_id": None}


@app.post("/api/wakeword/activation")
async def report_hardware_wake(request: Request) -> dict[str, str]:
    global _hardware_wake_event
    capture = "host"
    client_host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Wake events are accepted only from localhost")

    payload = await request.json()
    event_type = payload.get("event") if isinstance(payload, dict) else None
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if event_type == "detected":
        # The browser's capture path is far more accurate than host-mic capture on this hardware.
        if time.time() - _last_ui_status_poll < _UI_ALIVE_SECONDS:
            capture = "browser"
        _hardware_wake_event = {"session_id": None, "detected_at": time.time(), "capture": capture}
    elif event_type == "session_started" and isinstance(session_id, str) and session_id:
        detected_at = time.time()
        if _hardware_wake_event is not None and time.time() - float(_hardware_wake_event["detected_at"]) < 15:
            detected_at = float(_hardware_wake_event["detected_at"])
        _hardware_wake_event = {"session_id": session_id, "detected_at": detected_at}
    else:
        raise HTTPException(status_code=400, detail="event must be 'detected' or 'session_started' with a session_id")
    return {"status": "accepted", "capture": capture}


@app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(service: str, path: str, request: Request) -> Response:
    upstream = UPSTREAMS.get(service)
    if upstream is None:
        return Response(content=f"Unknown proxy target: {service}", status_code=404)

    target_url = f"{upstream}/{path}"
    body = await request.body()

    client: httpx.AsyncClient = request.app.state.client
    try:
        upstream_response = await client.request(
            request.method,
            target_url,
            params=request.query_params,
            headers=_filter_headers(request.headers),
            content=body,
        )
    except httpx.RequestError as exc:
        return Response(content=f"Upstream error: {exc}", status_code=502)

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_filter_headers(upstream_response.headers),
        media_type=upstream_response.headers.get("content-type"),
    )


if DIST_DIR.is_dir():
    # Serve hashed assets and other static files.
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        # SPA fallback: serve the requested file if it exists, else index.html.
        candidate = DIST_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST_DIR / "index.html")
else:  # pragma: no cover - only hit before the first build
    @app.get("/")
    def missing_build() -> Response:
        return Response(
            content=(
                "React UI is not built yet. Run 'npm install' and 'npm run build' "
                "inside kiosk-ui-react/."
            ),
            status_code=503,
        )


if __name__ == "__main__":
    host = os.getenv("KIOSK_UI_HOST", "0.0.0.0")
    port = int(os.getenv("KIOSK_UI_PORT", "7860"))
    uvicorn.run("ata_ui_server:app", host=host, port=port, reload=False)

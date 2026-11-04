"""App-level reverse proxy for the no-CORS backend services.

Reproduces, at the app origin, the same-origin passthrough the Vite dev proxy
provides in development so the *production* SPA (served from FastAPI, see
``_mount_spa`` in ``main.py``) can reach the content_search (``:9011``)
service without CORS.

Design — app-level httpx passthrough (not nginx/Caddy): the headless edge
appliance boots via ``setup.sh``/systemd, so an in-app proxy adds zero new
runtime components, keeps one origin/port/process, and is config-driven from
``config.content_search``. Request and response bodies are
streamed with ``httpx.AsyncClient.stream(...)`` → ``StreamingResponse`` so SSE,
multipart uploads, and large downloads pass through intact.

Scope — the ``/api/v1`` prefix collides: the main app owns ``/api/v1/sessions/*``
(``api/v1/api.py``) while content_search owns ``/api/v1/object|task|system/*``
(``:9011``). Only the content_search namespaces are proxied — **not** all of
``/api/v1`` — so the app's own ``/api/v1/sessions`` routes are never shadowed.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

logger = logging.getLogger(__name__)

# content_search (:9011) namespaces used by the SPA. Deliberately NOT
# "/api/v1" wholesale: "/api/v1/sessions" belongs to the main app.
_CONTENT_SEARCH_PREFIXES = ("/api/v1/object", "/api/v1/task", "/api/v1/system")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1). "host" is dropped
# too so httpx sets it from the upstream target URL. "content-length" is dropped
# on the *response* path because we re-stream the body.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)

# Top-level path segments the SPA catch-all fallback must not swallow. Derived
# from the proxy prefixes so ``main.py`` can extend the fallback's reserved set.
PROXY_RESERVED_SEGMENTS = frozenset(
    prefix.lstrip("/").split("/", 1)[0]
    for prefix in _CONTENT_SEARCH_PREFIXES
)


def _forward_request_headers(headers: Headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _forward_response_headers(headers: httpx.Headers) -> dict:
    drop = _HOP_BY_HOP | {"content-length"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _get_client(app: FastAPI) -> httpx.AsyncClient:
    """Lazily create (and cache) the shared upstream client on app state."""
    client = getattr(app.state, "proxy_client", None)
    if client is None or client.is_closed:
        # timeout=None: never truncate SSE streams / long uploads / large downloads.
        client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        app.state.proxy_client = client
    return client


async def _passthrough(request: Request, target_url: str) -> StreamingResponse:
    """Stream ``request`` to ``target_url`` and stream the reply straight back."""
    client = _get_client(request.app)

    # Only attach a request body when one is actually present, so bodyless
    # methods (GET/HEAD SSE) are not forced into chunked transfer encoding.
    has_body = (
        "content-length" in request.headers
        or request.headers.get("transfer-encoding") is not None
    )
    upstream_request = client.build_request(
        method=request.method,
        url=target_url,
        headers=_forward_request_headers(request.headers),
        content=request.stream() if has_body else None,
    )
    upstream = await client.send(upstream_request, stream=True)

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=_forward_response_headers(upstream.headers),
        background=BackgroundTask(upstream.aclose),
    )


def register_proxy_routes(app: FastAPI) -> None:
    """Register the content_search passthrough routes on ``app``.

    Must be called **after** ``register_routes(app)`` (so nothing is shadowed)
    and **before** the SPA catch-all mount (so the catch-all reserves these
    prefixes and never swallows them).
    """
    from utils.config_loader import config

    cs = getattr(config, "content_search", None)
    cs_host = str(getattr(cs, "host_addr", "127.0.0.1")) if cs is not None else "127.0.0.1"
    cs_port = int(getattr(cs, "port", 9011)) if cs is not None else 9011
    cs_base = f"http://{cs_host}:{cs_port}"

    # Create the shared client up front so concurrent first requests never race
    # to build one. Closed on shutdown via close_proxy_client().
    _get_client(app)

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

    async def content_search_proxy(request: Request):
        # Path is passed through verbatim (content_search serves the same
        # /api/v1/{object,task,system}/... paths); only the origin changes.
        target = f"{cs_base}{request.url.path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return await _passthrough(request, target)

    for prefix in _CONTENT_SEARCH_PREFIXES:
        app.add_api_route(
            f"{prefix}/{{full_path:path}}",
            content_search_proxy,
            methods=methods,
            include_in_schema=False,
        )

    logger.info(
        "Reverse proxy registered: %s -> %s",
        ", ".join(_CONTENT_SEARCH_PREFIXES),
        cs_base,
    )


async def close_proxy_client(app: FastAPI) -> None:
    """Close the shared upstream client on app shutdown (called from lifespan)."""
    client = getattr(app.state, "proxy_client", None)
    if client is not None and not client.is_closed:
        await client.aclose()


# ---------------------------------------------------------------------------
# Realtime transcription WebSocket passthrough (browser live-mic capture)
# ---------------------------------------------------------------------------
# The audio-analyzer exposes an OpenAI Realtime-compatible WebSocket at
# /v1/realtime. In the containerized topology the service is internal-only, so
# the SPA cannot reach it directly; this bridges the browser socket to the
# service over the internal network, mirroring the HTTP reverse proxy above.

def _audio_analyzer_ws_url(query: str = "") -> str:
    base = os.environ.get("AUDIO_ANALYZER_URL")
    if not base:
        from utils.config_loader import config
        aa = getattr(config, "audio_analyzer", None)
        host = str(getattr(aa, "host_addr", "127.0.0.1")) if aa is not None else "127.0.0.1"
        port = int(getattr(aa, "port", 8010)) if aa is not None else 8010
        base = f"http://{host}:{port}"
    ws_base = base.rstrip("/")
    if ws_base.startswith("https://"):
        ws_base = "wss://" + ws_base[len("https://"):]
    elif ws_base.startswith("http://"):
        ws_base = "ws://" + ws_base[len("http://"):]
    url = f"{ws_base}/v1/realtime"
    return f"{url}?{query}" if query else url


async def _realtime_ws_proxy(client_ws: WebSocket) -> None:
    """Bridge the browser realtime-transcription socket to the audio-analyzer."""
    import websockets

    upstream_url = _audio_analyzer_ws_url(client_ws.url.query)
    await client_ws.accept()
    try:
        async with websockets.connect(upstream_url, max_size=None, open_timeout=10) as upstream:

            async def client_to_upstream() -> None:
                while True:
                    message = await client_ws.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close()
                        return
                    text = message.get("text")
                    if text is not None:
                        await upstream.send(text)
                        continue
                    data = message.get("bytes")
                    if data is not None:
                        await upstream.send(data)

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if isinstance(message, (bytes, bytearray)):
                        await client_ws.send_bytes(bytes(message))
                    else:
                        await client_ws.send_text(message)

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - proxy is best-effort; log and close
        logger.warning("realtime WS proxy error (%s): %s", upstream_url, exc)
    finally:
        if client_ws.application_state != WebSocketState.DISCONNECTED:
            try:
                await client_ws.close()
            except Exception:
                pass


def register_realtime_ws_proxy(app: FastAPI) -> None:
    """Register the /v1/realtime WebSocket passthrough to the audio-analyzer."""
    app.add_api_websocket_route("/v1/realtime", _realtime_ws_proxy)
    logger.info("Realtime ASR WebSocket proxy registered: /v1/realtime -> audio-analyzer")

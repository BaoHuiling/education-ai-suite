"""Browser-facing VA stream URLs must point at the server the browser reached.

The configured mediamtx bases default to ``localhost``; a browser on another
machine that gets ``http://localhost:8889/front_stream`` dials itself and the
live player stays black.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.va as va_api
from components.va import va_service
from components.va.schemas import VideoAnalyticsRequest
from components.va.va_service import VideoAnalyticsService, _stream_base_url


@pytest.fixture
def stream_cfg(monkeypatch):
    cfg = {
        "stream_protocol": "webrtc",
        "webrtc_base_url": "http://localhost:8889",
        "hls_base_url": "http://localhost:8888",
    }
    monkeypatch.setattr(va_service, "_va_pipeline_cfg", lambda attr, default: cfg.get(attr, default))
    return cfg


@pytest.mark.parametrize(
    "client_host, expected",
    [
        (None, "http://localhost:8889"),
        ("localhost", "http://localhost:8889"),
        ("127.0.0.1", "http://localhost:8889"),
        ("::1", "http://localhost:8889"),
        ("10.239.97.252", "http://10.239.97.252:8889"),
        ("classroom-pc.local", "http://classroom-pc.local:8889"),
        ("fe80::1", "http://[fe80::1]:8889"),
    ],
)
def test_loopback_base_follows_client_host(stream_cfg, client_host, expected):
    assert _stream_base_url("webrtc", client_host) == expected


def test_hls_base_follows_client_host(stream_cfg):
    assert _stream_base_url("hls", "10.239.97.252") == "http://10.239.97.252:8888"


def test_explicit_config_host_is_kept(stream_cfg):
    stream_cfg["webrtc_base_url"] = "https://streams.example.com:9443"
    assert _stream_base_url("webrtc", "10.239.97.252") == "https://streams.example.com:9443"


def test_loopback_base_without_port(stream_cfg):
    stream_cfg["webrtc_base_url"] = "http://127.0.0.1"
    assert _stream_base_url("webrtc", "10.239.97.252") == "http://10.239.97.252"


class _FakeClient:
    def launch(self, *args, **kwargs):
        return True


def _service(monkeypatch, tmp_path):
    monkeypatch.setattr(va_service, "_set_va_stage", lambda *a, **k: None)
    monkeypatch.setattr(VideoAnalyticsService, "_va_dir", staticmethod(lambda sid: str(tmp_path)))
    monkeypatch.setattr(VideoAnalyticsService, "_ensure_watcher", lambda self, sid, sv: None)
    svc = VideoAnalyticsService(client=_FakeClient())
    return svc


def test_start_pipelines_stream_url_uses_client_host(stream_cfg, monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    req = VideoAnalyticsRequest(pipeline_name="front", source="rtsp://cam/live", output_stream=True)
    [result] = svc.start_pipelines("sess-1", [req], client_host="10.239.97.252")
    assert result["status"] == "success"
    assert result["stream_url"] == "http://10.239.97.252:8889/front_stream"
    assert result["stream_protocol"] == "webrtc"


def test_api_passes_request_host(stream_cfg, monkeypatch):
    seen = {}

    def fake_start(session_id, requests, client_host=None):
        seen["client_host"] = client_host
        return [{"status": "success", "pipeline_name": "front"}]

    monkeypatch.setattr(va_api, "get_service", lambda: SimpleNamespace(start_pipelines=fake_start))
    app = FastAPI()
    app.include_router(va_api.va_router)
    client = TestClient(app, base_url="http://10.239.97.252:8000")
    resp = client.post(
        "/start-video-analytics-pipeline",
        json=[{"pipeline_name": "front", "source": "rtsp://cam/live", "output_stream": True}],
        headers={"x-session-id": "sess-1"},
    )
    assert resp.status_code == 200, resp.text
    assert seen["client_host"] == "10.239.97.252"

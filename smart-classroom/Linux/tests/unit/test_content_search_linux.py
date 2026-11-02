# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from utils import compose_profiles


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ([], []),
        (["asr"], ["asr"]),
        (["summary"], ["asr", "text-gen"]),
        (["topic_segmentation"], ["asr", "text-gen"]),
        (["content_search"], ["text-gen"]),
        (["qa"], ["text-gen"]),
        (["video_analytics"], ["va"]),
        (["topic_segmentation", "video_analytics"], ["asr", "text-gen", "va"]),
    ],
)
def test_search_uses_app_and_existing_model_services(monkeypatch, features, expected):
    flags = SimpleNamespace(
        **{name: SimpleNamespace(enabled=True) for name in features}
    )
    monkeypatch.setattr(compose_profiles, "config", SimpleNamespace(features=flags))
    assert compose_profiles.resolve_profiles() == expected


def test_search_source_and_persistent_data_are_packaged():
    dockerfile = (ROOT / "docker/app.Dockerfile").read_text()
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    compose = yaml.safe_load((ROOT / "docker/docker-compose.yml").read_text())

    assert "COPY content_search/ ./content_search/" in dockerfile
    assert "ln -s /app/storage/content-search /app/content_search/data" in dockerfile
    assert "USER app" in dockerfile
    assert "content_search/data/" in ignore
    assert "content_search/logs/" in ignore

    services = compose["services"]
    assert "/app/storage/content-search" in services["storage-permissions"]["command"]
    assert any(v.endswith(":/app/storage") for v in services["app"]["volumes"])
    assert "HF_HOME=/app/models/huggingface" in services["app"]["environment"]
    assert services["app"]["ports"] == ["8000:8000"]


def test_dev_ui_uses_same_origin_app_proxy():
    vite = (ROOT / "ui/vite.config.ts").read_text()
    assert "'/api/v1': {\n        target: 'http://127.0.0.1:8000'" in vite


def test_search_defaults_do_not_require_app_gpu_runtime():
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8-sig"))
    ingest = config["content_search"]["file_ingest"]
    assert ingest["doc_embedding_device"] == "CPU"
    assert ingest["reranker"]["device"] == "CPU"


def test_setup_waits_for_search_before_starting_ui():
    setup = (ROOT / "setup.sh").read_text()
    assert 'for name in ("topic_segmentation", "content_search", "qa")' in setup
    assert setup.index("/api/v1/system/health") < setup.index('UI_DIR="${SCRIPT_DIR}/ui"')
    assert 'die "Content Search did not become ready' in setup

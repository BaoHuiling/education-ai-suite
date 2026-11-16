# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from types import SimpleNamespace

import yaml

from utils import compose_profiles


ROOT = Path(__file__).resolve().parents[2]


def _features(*names):
    flags = SimpleNamespace(**{name: SimpleNamespace(enabled=True) for name in names})
    return SimpleNamespace(features=flags)


def test_write_env_adds_startup_models_config_when_text_gen_active(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("HF_TOKEN=abc\n", encoding="utf-8")
    monkeypatch.setattr(compose_profiles, "config", _features("summary"))

    assert compose_profiles.write_env(env) == "asr,text-gen"
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "HF_TOKEN=abc" in lines
    assert "COMPOSE_PROFILES=asr,text-gen" in lines
    assert "STARTUP_MODELS_CONFIG=/opt/startup-models.yaml" in lines


def test_write_env_keeps_custom_startup_models_config(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("STARTUP_MODELS_CONFIG=/opt/custom.yaml\nCOMPOSE_PROFILES=va\n", encoding="utf-8")
    monkeypatch.setattr(compose_profiles, "config", _features("qa"))

    compose_profiles.write_env(env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines.count("STARTUP_MODELS_CONFIG=/opt/custom.yaml") == 1
    assert "COMPOSE_PROFILES=text-gen" in lines


def test_write_env_removes_startup_models_config_for_asr_only(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("STARTUP_MODELS_CONFIG=/opt/startup-models.yaml\nRENDER_GID=1\n", encoding="utf-8")
    monkeypatch.setattr(compose_profiles, "config", _features("asr"))

    assert compose_profiles.write_env(env) == "asr"
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "RENDER_GID=1" in lines
    assert "COMPOSE_PROFILES=asr" in lines
    assert not any(line.startswith("STARTUP_MODELS_CONFIG=") for line in lines)


def test_asr_shares_single_model_downloader_container():
    compose = yaml.safe_load((ROOT / "docker/docker-compose.yml").read_text())
    services = compose["services"]
    downloader = services["model-downloader"]
    analyzer = services["audio-analyzer"]

    assert set(downloader["profiles"]) == {"asr", "text-gen"}
    assert set(services["model-cache-permissions"]["profiles"]) == {"asr", "text-gen"}
    assert "modelscope" in downloader["command"]
    # Value-less so an absent .env line leaves it unset (no OVMS exports).
    assert "STARTUP_MODELS_CONFIG" in downloader["environment"]
    assert not any(e.startswith("STARTUP_MODELS_CONFIG=") for e in downloader["environment"])

    assert analyzer["environment"]["AUDIO_ANALYZER_MODEL_DOWNLOAD_URL"] == "http://model-downloader:8000"
    assert "model-downloader" in analyzer["environment"]["NO_PROXY"]
    assert analyzer["depends_on"]["model-downloader"]["condition"] == "service_healthy"
    assert "model-cache:/app/audio_analyzer/models" in analyzer["volumes"]
    assert "model-cache:/opt/models" in downloader["volumes"]
    assert "audio-analyzer-models" not in compose["volumes"]
    assert not any(v.startswith("audio-analyzer-models:") for v in analyzer["volumes"])

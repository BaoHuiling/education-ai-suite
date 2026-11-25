# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

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
    assert analyzer["environment"]["SMART_CLASSROOM_CONFIG_PATH"] == "/opt/smart-classroom/config.yaml"
    assert analyzer["entrypoint"] == ["python3.12", "/opt/smart-classroom/audio_analyzer_config.py"]
    assert "../config.yaml:/opt/smart-classroom/config.yaml:ro" in analyzer["volumes"]
    assert "./audio_analyzer_config.py:/opt/smart-classroom/audio_analyzer_config.py:ro" in analyzer["volumes"]
    assert "model-downloader" in analyzer["environment"]["NO_PROXY"]
    assert analyzer["depends_on"]["model-downloader"]["condition"] == "service_healthy"
    assert "model-cache:/app/audio_analyzer/models" in analyzer["volumes"]
    assert "model-cache:/opt/models" in downloader["volumes"]
    assert "audio-analyzer-models" not in compose["volumes"]
    assert not any(v.startswith("audio-analyzer-models:") for v in analyzer["volumes"])


class TestAudioAnalyzerConfig(unittest.TestCase):
    def test_forwards_asr_settings_without_overwriting_other_settings(self):
        for provider, model in (("funasr", "paraformer-zh"), ("openvino", "whisper-base")):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(yaml.safe_dump({
                    "models": {"asr": {
                        "provider": provider, "name": model, "device": "CPU", "diarization": False,
                        "temperature": 0.5, "no_speech_threshold": 0.6, "logprob_threshold": -1.0,
                        "min_duration_sec": 0.25, "min_words": 2, "models_base_path": "not-forwarded",
                    }},
                }), encoding="utf-8")
                environment = os.environ.copy()
                environment.update({
                    "SMART_CLASSROOM_CONFIG_PATH": str(path),
                    "AUDIO_ANALYZER__MODELS__ASR__PROVIDER": "old-provider",
                    "AUDIO_ANALYZER__MODELS__ASR__MODELS_BASE_PATH": "analyzer-models",
                })
                result = subprocess.run(
                    [sys.executable, str(ROOT / "docker/audio_analyzer_config.py"), sys.executable, "-c",
                     "import json, os; print(json.dumps({key: value for key, value in os.environ.items() "
                     "if key.startswith('AUDIO_ANALYZER__MODELS__ASR__')}))"],
                    env=environment, capture_output=True, text=True, check=True,
                )
                values = json.loads(result.stdout)
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__PROVIDER"], provider)
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__NAME"], model)
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__DEVICE"], "CPU")
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__DIARIZATION"], "False")
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__TEMPERATURE"], "0.5")
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__NO_SPEECH_THRESHOLD"], "0.6")
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__LOGPROB_THRESHOLD"], "-1.0")
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__MIN_DURATION_SEC"], "0.25")
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__MIN_WORDS"], "2")
                self.assertEqual(values["AUDIO_ANALYZER__MODELS__ASR__MODELS_BASE_PATH"], "analyzer-models")

    def test_missing_model_settings_fail_without_starting_analyzer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump({
                "models": {"asr": {"provider": "funasr", "device": "CPU"}},
            }), encoding="utf-8")
            environment = os.environ.copy()
            environment["SMART_CLASSROOM_CONFIG_PATH"] = str(path)
            result = subprocess.run(
                [sys.executable, str(ROOT / "docker/audio_analyzer_config.py"), sys.executable, "-c",
                 "print('analyzer started')"],
                env=environment, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("models.asr.name must be a non-empty string", result.stderr)
            self.assertNotIn("analyzer started", result.stdout)


def test_report_alone_needs_asr_and_text_gen(monkeypatch):
    # report consumes ASR-derived outputs and calls text_gen; it must not pull
    # in the opt-in VA side-car profile.
    monkeypatch.setattr(compose_profiles, "config", _features("report"))
    assert compose_profiles.resolve_profiles() == ["asr", "text-gen"]

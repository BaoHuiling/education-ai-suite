# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import yaml

from utils import config_loader, ov_runtime


def test_gpu_queue_priority_defaults_to_low(monkeypatch):
    monkeypatch.setattr(
        config_loader,
        "config",
        SimpleNamespace(models=SimpleNamespace()),
    )

    assert ov_runtime.gpu_queue_priority() == "LOW"


def test_gpu_queue_priority_accepts_configured_values(monkeypatch):
    monkeypatch.setattr(
        config_loader,
        "config",
        SimpleNamespace(models=SimpleNamespace(gpu_queue_priority="high")),
    )

    assert ov_runtime.gpu_queue_priority() == "HIGH"


def test_gpu_queue_priority_warns_and_defaults_for_invalid_value(monkeypatch, caplog):
    monkeypatch.setattr(
        config_loader,
        "config",
        SimpleNamespace(models=SimpleNamespace(gpu_queue_priority="urgent")),
    )

    with caplog.at_level("WARNING", logger="utils.ov_runtime"):
        assert ov_runtime.gpu_queue_priority() == "LOW"

    assert "Invalid GPU queue priority 'urgent'; falling back to LOW" in caplog.text


def test_gpu_plugin_config_applies_priority_only_to_gpu(monkeypatch):
    monkeypatch.setattr(
        config_loader,
        "config",
        SimpleNamespace(models=SimpleNamespace(gpu_queue_priority="MEDIUM")),
    )
    base = {"GPU_ENABLE_LARGE_ALLOCATIONS": "YES"}

    gpu_config = ov_runtime.gpu_plugin_config("GPU", base)
    cpu_config = ov_runtime.gpu_plugin_config("CPU", base)
    npu_config = ov_runtime.gpu_plugin_config("NPU", base)

    assert gpu_config == {
        "GPU_ENABLE_LARGE_ALLOCATIONS": "YES",
        "GPU_QUEUE_PRIORITY": "MEDIUM",
    }
    assert cpu_config == base
    assert npu_config == base
    assert base == {"GPU_ENABLE_LARGE_ALLOCATIONS": "YES"}


def test_va_device_normalized_during_config_load(monkeypatch, tmp_path, caplog):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("SC_CONFIG_PATH", str(config_path))

    for configured in ("GPU", "CPU", "NPU", "gpu", "cpu", "npu"):
        caplog.clear()
        config_path.write_text(yaml.safe_dump({"models": {"va": {"device": configured}}}), encoding="utf-8")
        loaded = config_loader.load_config()
        assert loaded.models.va.device == configured.upper()
        assert "Invalid VA device" not in caplog.text


def test_invalid_va_device_warns_and_defaults_during_config_load(monkeypatch, tmp_path, caplog):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("SC_CONFIG_PATH", str(config_path))

    for configured in ("INVALID_DEVICE", "GPU.0", "", None, 123, False, [], {}):
        caplog.clear()
        content = yaml.safe_dump({"models": {"va": {"device": configured, "threshold": 0.5}}})
        config_path.write_text(content, encoding="utf-8")
        with caplog.at_level("WARNING", logger="utils.config_loader"):
            loaded = config_loader.load_config()
        assert loaded.models.va.device == "NPU"
        assert loaded.models.va.threshold == 0.5
        assert f"Invalid VA device {configured!r}; falling back to NPU" in caplog.text
        assert "Valid values are: GPU, NPU, CPU" in caplog.text
        assert config_path.read_text(encoding="utf-8") == content


def test_missing_va_device_defaults_without_warning(monkeypatch, tmp_path, caplog):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models:\n  va: {}\n", encoding="utf-8")
    monkeypatch.setenv("SC_CONFIG_PATH", str(config_path))

    assert config_loader.load_config().models.va.device == "NPU"
    assert "Invalid VA device" not in caplog.text


def test_missing_va_config_is_not_added(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\n", encoding="utf-8")
    monkeypatch.setenv("SC_CONFIG_PATH", str(config_path))

    assert not hasattr(config_loader.load_config().models, "va")
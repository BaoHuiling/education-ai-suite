# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

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
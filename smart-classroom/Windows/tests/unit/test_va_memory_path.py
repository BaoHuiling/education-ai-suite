# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""GPU/NPU pipelines stay in D3D12 memory when DL Streamer supports it.

DL Streamer 2026.3+ (use_d3d12) takes D3D12Memory into gvadetect/gvaclassify
and gvawatermark, so frames go from the d3d12 decoder to d3d12h264enc without
a copy. Older installs copy to D3D11 after the decoder (d3d12download) and
encode with mfh264enc. device=CPU reads system memory on both.

The service is built with object.__new__, as in test_va_pipeline_stop.py.
"""

from pathlib import Path

import pytest

from components.va.va_pipeline_service import (
    RESUME_PROBE_ELEMENT,
    PipelineOptions,
    VideoAnalyticsPipelineService,
)


def make_service(use_d3d12):
    svc = object.__new__(VideoAnalyticsPipelineService)
    svc.use_d3d12 = use_d3d12
    svc.front_input_size = None
    svc.front_inference_interval = 1
    svc.front_threshold = None
    svc.max_input_framerate = "15/1"
    svc.classify_framerate = "1/1"
    svc.model_base_dir = Path("C:/m")
    svc.models = {"front-pose": "f.xml", "back-pose": "b.xml", "resnet18": "r.xml", "reid": "reid.xml"}
    return svc


def build(svc, name, device, tmp_path):
    opts = PipelineOptions(device=device, output_dir=str(tmp_path), output_stream=True)
    return getattr(svc, f"_build_pipeline_{name}")("C:/v/in.mp4", opts, "file")


def after_decoder(tokens):
    i = tokens.index("decodebin3")
    j = tokens.index(f"name={RESUME_PROBE_ELEMENT}")
    return tokens[i + 1 : j + 1]


def test_legacy_service_defaults_to_d3d11():
    assert VideoAnalyticsPipelineService.use_d3d12 is False


@pytest.mark.parametrize("name", ["front", "back", "content"])
@pytest.mark.parametrize("device", ["GPU", "NPU"])
def test_d3d12_end_to_end(name, device, tmp_path):
    toks = build(make_service(True), name, device, tmp_path)
    assert after_decoder(toks) == ["!", "video/x-raw(memory:D3D12Memory)", "!", "identity", f"name={RESUME_PROBE_ELEMENT}"]
    assert "d3d12download" not in toks
    assert not any("D3D11Memory" in t for t in toks)
    backends = {t for t in toks if t.startswith("pre-process-backend=")}
    assert backends == {"pre-process-backend=d3d12"}
    enc = toks.index("d3d12h264enc")
    assert toks[enc - 4] == "d3d12convert"
    assert "rate-control=cqp" in toks[enc:]
    assert "mfh264enc" not in toks


@pytest.mark.parametrize("name", ["front", "back", "content"])
def test_d3d11_on_older_dlstreamer(name, tmp_path):
    toks = build(make_service(False), name, "NPU", tmp_path)
    assert after_decoder(toks)[-2:] == ["d3d12download", f"name={RESUME_PROBE_ELEMENT}"]
    assert not any("D3D12Memory" in t for t in toks[toks.index(f"name={RESUME_PROBE_ELEMENT}"):])
    assert {t for t in toks if t.startswith("pre-process-backend=")} == {"pre-process-backend=d3d11"}
    assert "mfh264enc" in toks and "d3d12h264enc" not in toks


@pytest.mark.parametrize("use_d3d12, upload", [(True, "d3d12upload"), (False, "d3d11upload")])
def test_cpu_reads_system_memory_and_uploads_for_encode(use_d3d12, upload, tmp_path):
    toks = build(make_service(use_d3d12), "front", "CPU", tmp_path)
    assert after_decoder(toks)[-2:] == ["d3d12download", f"name={RESUME_PROBE_ELEMENT}"]
    assert {t for t in toks if t.startswith("pre-process-backend=")} == {"pre-process-backend=opencv"}
    i = toks.index(upload)
    assert toks[i + 2] in ("d3d12convert", "d3d11convert")


def test_no_upload_without_stream(tmp_path):
    opts = PipelineOptions(device="CPU", output_dir=str(tmp_path), output_stream=False)
    toks = make_service(True)._build_pipeline_back("C:/v/in.mp4", opts, "file")
    assert "d3d12upload" not in toks and toks[-3:] == ["fakesink", "async=false", "sync=false"]


@pytest.mark.parametrize(
    "outputs, expected",
    [
        ({"gvadetect": "video/x-raw(memory:D3D11Memory)", "gvawatermark": "", "d3d12h264enc": ""}, False),
        ({"gvadetect": "memory:D3D12Memory", "gvawatermark": "memory:D3D11Memory", "d3d12h264enc": ""}, False),
        ({"gvadetect": "memory:D3D12Memory", "gvawatermark": "memory:D3D12Memory"}, False),
        ({"gvadetect": "memory:D3D12Memory", "gvawatermark": "memory:D3D12Memory", "d3d12h264enc": ""}, True),
    ],
)
def test_dlstreamer_supports_d3d12(monkeypatch, outputs, expected):
    """Decided from gst-inspect sink caps, not the version: 2026.3.0-rc reports 2026.2.0."""
    import subprocess
    from types import SimpleNamespace

    from utils import system_checker

    def fake_run(cmd, **kwargs):
        element = cmd[1]
        if element not in outputs:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=outputs[element])

    monkeypatch.setattr(subprocess, "run", fake_run)
    system_checker.dlstreamer_supports_d3d12.cache_clear()
    try:
        assert system_checker.dlstreamer_supports_d3d12() is expected
    finally:
        system_checker.dlstreamer_supports_d3d12.cache_clear()

# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Front pose detection runs at full resolution on every Nth frame.

Back-row students are only tens of pixels tall, so the front pose model is
reshaped to front_input_size instead of downscaling to its native input, and
inferred every front_inference_interval-th frame with gvatrack carrying the
boxes in between (gvaposturedetect holds their postures).

The service is built with object.__new__, as in test_va_pipeline_stop.py.
"""

import pytest

from components.va.va_pipeline_service import PipelineOptions, VideoAnalyticsPipelineService


def make_service(input_size=None, interval=1, threshold=None):
    svc = object.__new__(VideoAnalyticsPipelineService)
    svc.front_input_size = input_size
    svc.front_inference_interval = interval
    svc.front_threshold = threshold
    svc.max_input_framerate = "15/1"
    svc.use_d3d12 = False
    svc.model_base_dir = __import__("pathlib").Path("C:/m")
    svc.models = {"front-pose": "yolov8m-pose.xml", "reid": "reid.xml"}
    return svc


def detect_segment(svc, tmp_path, threshold=0.5):
    opts = PipelineOptions(device="GPU", output_dir=str(tmp_path), output_stream=False, threshold=threshold)
    toks = svc._build_pipeline_front("C:/v/in.mp4", opts, "file")
    i = toks.index("gvadetect")
    j = toks.index("gvaposturedetect")
    return toks[i:j]


def test_defaults_keep_native_input_and_every_frame(tmp_path):
    seg = detect_segment(make_service(), tmp_path)
    assert "threshold=0.5" in seg
    assert not any(t.startswith(("reshape", "inference-interval")) for t in seg)
    assert "gvatrack" not in seg


def test_full_resolution_every_third_frame_with_tracker(tmp_path):
    seg = detect_segment(make_service("1920x1088", 3, 0.3), tmp_path)
    assert "threshold=0.3" in seg
    assert ["reshape=true", "reshape-width=1920", "reshape-height=1088"] == \
        [t for t in seg if t.startswith("reshape")]
    assert "inference-interval=3" in seg
    # gvatrack sits between gvadetect and gvaposturedetect, which holds postures by track id
    assert seg[-3:] == ["gvatrack", "tracking-type=short-term-imageless", "!"]


def test_reshape_without_interval_has_no_tracker(tmp_path):
    seg = detect_segment(make_service("1280x736", 1), tmp_path)
    assert "reshape-width=1280" in seg and "reshape-height=736" in seg
    assert "gvatrack" not in seg


@pytest.mark.parametrize("device", ["CPU", "GPU", "NPU"])
@pytest.mark.parametrize("stream_name", ["front_stream", "back_stream", "content_stream"])
@pytest.mark.parametrize("use_d3d12", [False, True])
def test_video_sink_uploads_only_cpu_frames(device, stream_name, use_d3d12):
    service = make_service()
    service.use_d3d12 = use_d3d12
    options = PipelineOptions(device=device)
    elements = service._get_video_sink_elements(options, stream_name, "15/1")
    expected_sink = service._get_rtsp_sink_elements(options.output_rtsp, stream_name, "15/1")
    backend = "d3d12" if use_d3d12 else "d3d11"
    uploads = [element for element in elements if element in ("d3d11upload", "d3d12upload")]

    if device == "CPU":
        assert elements == [f"{backend}upload", "!", *expected_sink]
        assert elements[:3] == [f"{backend}upload", "!", f"{backend}convert"]
        assert uploads == [f"{backend}upload"]
        assert service._memory_caps(options) == "video/x-raw"
        assert service._get_preprocess_backend_elements(options) == ["pre-process-backend=opencv"]
    else:
        assert elements == expected_sink
        assert uploads == []


@pytest.mark.parametrize("device", ["CPU", "GPU", "NPU"])
@pytest.mark.parametrize("use_d3d12", [False, True])
def test_disabled_video_stream_does_not_upload_frames(device, use_d3d12):
    service = make_service()
    service.use_d3d12 = use_d3d12
    options = PipelineOptions(device=device, output_stream=False)
    assert service._get_video_sink_elements(options, "content_stream", "15/1") == [
        "fakesink", "async=false", "sync=false"
    ]

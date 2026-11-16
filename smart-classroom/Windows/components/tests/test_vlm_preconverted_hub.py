# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Pre-converted OpenVINO IR comes from models.model_hub (identical files on both hubs).

Kept apart from test_model_hub.py: importing the VLM serving utils reconfigures root
logging, which breaks caplog-based tests collected after it (test_text_chunker & co).
"""

import importlib
import os
import sys

import huggingface_hub
import pytest

_SC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)

from utils import model_download_helper as helper
from utils import model_hub

ms_download = importlib.import_module("modelscope.hub.snapshot_download")


@pytest.fixture
def downloads(monkeypatch):
    calls = []

    def fake(hub):
        def download(repo_id, revision=None, local_dir=None, **kwargs):
            calls.append((hub, repo_id, kwargs.get("allow_patterns")))
            os.makedirs(local_dir, exist_ok=True)
        return download

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake("hf"))
    monkeypatch.setattr(ms_download, "snapshot_download", fake("ms"))
    return calls


@pytest.mark.parametrize("hub", ["hf", "ms"])
def test_preconverted_ir_comes_from_the_configured_hub(tmp_path, monkeypatch, downloads, hub):
    # Appended, as text_gen_prepare._convert_helpers does: the serving utils import
    # content_search providers, and content_search has a utils package of its own.
    monkeypatch.setattr(sys, "path", sys.path + [os.path.join(_SC_ROOT, "content_search")])
    from components.vlm.vlm_openvino_serving.utils import utils as vlm_utils

    monkeypatch.setattr(model_hub, "get_model_hub", lambda: hub)
    monkeypatch.setattr(helper, "get_model_hub", lambda: hub)
    vlm_utils._download_preconverted_ov_model("OpenVINO/Qwen3.5-9B-int4-ov", str(tmp_path / "ir"))
    [(got_hub, repo, patterns)] = downloads
    assert (got_hub, repo) == (hub, "OpenVINO/Qwen3.5-9B-int4-ov")
    assert "openvino_*.bin" in patterns

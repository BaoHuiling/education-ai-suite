# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from utils import config_loader, ovms_provision, sync_startup_models


ROOT = Path(__file__).resolve().parents[2]
PIPELINE_TYPES = ["LM", "LM_CB", "VLM", "VLM_CB", "AUTO"]


@pytest.mark.parametrize("pipeline_type", PIPELINE_TYPES)
@pytest.mark.parametrize("cpu_safe", [True, False])
def test_sync_passes_pipeline_type_to_downloader(monkeypatch, pipeline_type, cpu_safe):
    config = deepcopy(sync_startup_models.config)
    config.models.text_gen.ovms.pipeline_type = pipeline_type
    monkeypatch.setattr(sync_startup_models, "config", config)
    data = {
        "models": [
            {"type": "vlm", "config": {"pipeline_type": "stale", "cache_size": 4}},
            {"type": "embeddings", "config": {"device": "CPU"}},
        ]
    }

    sync_startup_models._apply(
        data, sync_startup_models._wanted_names(), keep_vlm_cpu_safe=cpu_safe
    )

    vlm = data["models"][0]
    assert vlm["config"]["pipeline_type"] == pipeline_type
    assert "pipeline_type" not in vlm
    assert vlm["config"]["cache_size"] == 4
    expected_device = "CPU" if cpu_safe else config.models.text_gen.device
    assert vlm["config"]["device"] == expected_device
    assert "pipeline_type" not in data["models"][1]["config"]
    assert not sync_startup_models._apply(
        data, sync_startup_models._wanted_names(), keep_vlm_cpu_safe=cpu_safe
    )


@pytest.mark.parametrize("pipeline_type", PIPELINE_TYPES)
def test_generated_startup_config_preserves_pipeline_type(monkeypatch, tmp_path, pipeline_type):
    config = deepcopy(ovms_provision.config)
    config.models.text_gen.ovms.pipeline_type = pipeline_type
    monkeypatch.setattr(ovms_provision, "config", config)
    path = tmp_path / "startup-models.yaml"

    ovms_provision.write_startup_config(ovms_provision.build_specs(), path)

    models = yaml.safe_load(path.read_text())["models"]
    assert models[0]["config"]["pipeline_type"] == pipeline_type
    assert all("pipeline_type" not in entry["config"] for entry in models[1:])


def test_default_pipeline_type_matches_downloader(monkeypatch):
    config = deepcopy(ovms_provision.config)
    config.models.text_gen.ovms = SimpleNamespace()
    monkeypatch.setattr(ovms_provision, "config", config)
    monkeypatch.setattr(sync_startup_models, "config", config)
    assert ovms_provision._text_gen_spec().pipeline_type == "VLM"
    assert sync_startup_models._wanted_names()["vlm"]["pipeline_type"] == "VLM"


@pytest.mark.parametrize("pipeline_type", PIPELINE_TYPES)
def test_valid_pipeline_types(pipeline_type):
    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8-sig"))
    data["models"]["text_gen"]["ovms"]["pipeline_type"] = pipeline_type
    config_loader._validate_text_gen(data)


@pytest.mark.parametrize("pipeline_type", [None, "", "vlm", "invalid", 3, ["VLM"]])
def test_invalid_pipeline_types_fail_explicitly(pipeline_type):
    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8-sig"))
    data["models"]["text_gen"]["ovms"]["pipeline_type"] = pipeline_type
    with pytest.raises(ValueError, match=r"models.text_gen.ovms.pipeline_type"):
        config_loader._validate_text_gen(data)


def test_checked_in_startup_configs_match_vlm_setting():
    assert config_loader.config.models.text_gen.ovms.pipeline_type == "VLM"
    for path in sync_startup_models.STARTUP_MODELS_FILES:
        models = yaml.safe_load(path.read_text())["models"]
        vlm = next(entry for entry in models if entry["type"] == "vlm")
        assert vlm["config"]["pipeline_type"] == "VLM"

# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""config.yaml `REGISTRY` flag: Docker Hub pull (true) vs local build (false)."""

from pathlib import Path

import pytest
import yaml

from utils.registry_mode import DEFAULT_REGISTRY, parse_registry

_ROOT = Path(__file__).resolve().parents[2]
_DOCKER_DIR = _ROOT / "docker"


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("False", False),
        (" TRUE ", True),
        (None, DEFAULT_REGISTRY),
    ],
)
def test_parse_registry_accepts_booleans(value, expected):
    assert parse_registry(value) is expected


@pytest.mark.parametrize("value", ["maybe", "", 0, 1, [], {}])
def test_parse_registry_rejects_non_booleans(value):
    with pytest.raises(ValueError, match="REGISTRY"):
        parse_registry(value)


def test_config_yaml_declares_registry_flag():
    data = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8-sig"))
    assert isinstance(data.get("REGISTRY"), bool)


@pytest.mark.parametrize("name", ["audio-analyzer", "model-downloader", "metrics-collector"])
def test_local_build_override_replaces_pulled_image(name):
    """Each Docker Hub service builds locally (never pulls) under REGISTRY: false."""
    base = yaml.safe_load((_DOCKER_DIR / "docker-compose.yml").read_text(encoding="utf-8"))
    local = yaml.safe_load((_DOCKER_DIR / "docker-compose.local-build.yml").read_text(encoding="utf-8"))

    assert "build" not in base["services"][name]
    override = local["services"][name]
    assert "build" in override
    assert str(override["image"]).endswith(":local")
    assert override["pull_policy"] == "build"

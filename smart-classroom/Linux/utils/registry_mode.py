# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Resolve the config.yaml `REGISTRY` flag that selects the Docker image source.

  REGISTRY: true   -> pull the published Docker Hub images
                      (intel/audio-analyzer, intel/model-download,
                      intel/retail-benchmark)
  REGISTRY: false  -> build those images locally from the submodules by adding
                      docker/docker-compose.local-build.yml to the Compose command

A missing flag means `true`. setup.sh consumes this::

    python3 -m utils.registry_mode      # prints "true" or "false"
"""

from __future__ import annotations

import sys
from typing import Any

DEFAULT_REGISTRY = True


def parse_registry(value: Any) -> bool:
    """Normalize a raw `REGISTRY` value; raise ValueError if it is not a boolean."""
    if value is None:
        return DEFAULT_REGISTRY
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"config.yaml REGISTRY must be true or false, got {value!r}.")


def registry_enabled() -> bool:
    from utils.config_loader import config

    return parse_registry(getattr(config, "REGISTRY", None))


def main() -> int:
    try:
        print("true" if registry_enabled() else "false")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Derive docker-compose `profiles:` from config.yaml `features:` flags.

Feature modularity (`model_manager/feature_bootstrap.py` + `config.yaml`
`features:`) only ever gated which routers/capabilities build *inside* the
`app` container — it had no effect on which sibling containers
(`audio-analyzer`, `model-downloader`, `ovms-provision`, `ovms`) a bare
`docker compose up` starts, since none of them
declared a Compose `profiles:` key. This module closes that gap: it maps each
enabled feature to the Compose profile(s) that back it, and writes the
resulting `COMPOSE_PROFILES` value to `.env` so `docker compose up` only
starts the containers the enabled features actually need.

Profile groups (must match the `profiles:` lists in docker/docker-compose.yml):
  - "asr"            -> audio-analyzer + the shared model-downloader (ASR snapshots)
  - "text-gen"       -> model-downloader, ovms-provision, ovms (VLM serving)

`model-downloader` is shared by both groups. Its OVMS exports are driven by
`STARTUP_MODELS_CONFIG`, which this module also writes to `.env` -- only when
`text-gen` is active -- so an ASR-only stack does not download the VLM,
embedding and reranker models it will never serve (docker-compose.yml declares
the variable value-less so an absent `.env` line leaves it unset in the
container).

Content Search is launched inside app by ContentSearchFeature, not by a
separate Compose service. Its models use the same persistent model cache.

A feature maps to a profile if it directly needs that capability, OR
transitively depends on another feature that does (mirrors
`model_manager/features/*`'s own `requires`/`depends_on` declarations, kept
here as a small static table since compose-time profile resolution happens
before the Python app/its feature registry is even running).

Run standalone::

    python -m utils.compose_profiles                 # print COMPOSE_PROFILES=... to stdout
    python -m utils.compose_profiles --write-env      # (re)write/update .env with it
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Dict, FrozenSet, List

from utils.config_loader import config

logger = logging.getLogger(__name__)

SC_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = SC_ROOT / ".env"

# Container path of the startup-models bind mount in docker/docker-compose.yml.
STARTUP_MODELS_CONFIG_KEY = "STARTUP_MODELS_CONFIG"
STARTUP_MODELS_CONFIG_DEFAULT = "/opt/startup-models.yaml"

# feature id (config.yaml `features:` key) -> compose profile(s) it requires.
# asr: direct transcription feature AND every feature downstream of it in the
# pipeline (summary/mindmap/topic_segmentation/qa all consume ASR output).
# text-gen: every feature that calls the text_gen VLM capability.
_FEATURE_PROFILES: Dict[str, FrozenSet[str]] = {
    "asr":                 frozenset({"asr"}),
    "summary":             frozenset({"asr", "text-gen"}),
    "mindmap":             frozenset({"asr", "text-gen"}),
    "topic_segmentation":  frozenset({"asr", "text-gen"}),
    "content_search":      frozenset({"text-gen"}),
    "qa":                  frozenset({"text-gen"}),
    "video_analytics":     frozenset({"va"}),
}

# Known profile names, for validating/ordering output deterministically.
_ALL_PROFILES: List[str] = ["asr", "text-gen", "va"]


def _enabled_features() -> List[str]:
    features = getattr(config, "features", None)
    if features is None:
        return []
    enabled = []
    for name in _FEATURE_PROFILES:
        block = getattr(features, name, None)
        if block is not None and bool(getattr(block, "enabled", False)):
            enabled.append(name)
    return enabled


def resolve_profiles() -> List[str]:
    """Return the sorted, deterministic list of Compose profiles to activate
    for the currently enabled `config.yaml` features."""
    active: set = set()
    for feature in _enabled_features():
        active |= _FEATURE_PROFILES.get(feature, frozenset())
    return [p for p in _ALL_PROFILES if p in active]


def write_env(path: Path = ENV_PATH) -> str:
    """(Re)write the `COMPOSE_PROFILES=` line in `.env`, preserving any other
    lines already present. Returns the value written.

    Also manages `STARTUP_MODELS_CONFIG=`: kept (added with the default if
    missing) while `text-gen` is active, removed otherwise."""
    profiles = resolve_profiles()
    value = ",".join(profiles)
    text_gen = "text-gen" in profiles
    lines: List[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    replaced = False
    startup_present = False
    new_lines: List[str] = []
    for line in lines:
        if line.startswith("COMPOSE_PROFILES="):
            new_lines.append(f"COMPOSE_PROFILES={value}")
            replaced = True
        elif line.startswith(f"{STARTUP_MODELS_CONFIG_KEY}="):
            if text_gen:
                new_lines.append(line)
                startup_present = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"COMPOSE_PROFILES={value}")
    if text_gen and not startup_present:
        new_lines.append(f"{STARTUP_MODELS_CONFIG_KEY}={STARTUP_MODELS_CONFIG_DEFAULT}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    logger.info("✅ Wrote COMPOSE_PROFILES=%s to %s", value, path)
    logger.info(
        "   %s %s",
        STARTUP_MODELS_CONFIG_KEY,
        "set (text-gen active: model-downloader exports OVMS models)" if text_gen
        else "unset (text-gen inactive: model-downloader serves ASR snapshots only)",
    )
    return value


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Derive docker-compose COMPOSE_PROFILES from config.yaml features:."
    )
    parser.add_argument(
        "--write-env", action="store_true",
        help="Write/update COMPOSE_PROFILES in .env instead of printing to stdout.",
    )
    args = parser.parse_args()

    if args.write_env:
        write_env()
    else:
        print(f"COMPOSE_PROFILES={','.join(resolve_profiles())}")


if __name__ == "__main__":
    main()

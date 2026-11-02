# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Sync components/vlm/config/startup-models(.gpu).yaml model names/params
from config.yaml, so the two hand-authored files can't silently drift from
what the app is actually configured to use.

config.yaml is the single source of truth for *which* models are wanted:
  - models.text_gen.vlm_name / weight_format / ovms.pipeline_type -> the `vlm` entry
  - models.embedding.name                             -> the `embeddings` entry
  - content_search.reranker.model                     -> the `rerank` entry

startup-models.yaml / startup-models.gpu.yaml are what model-downloader
actually reads to export models for OVMS. This script only overwrites the
`name` field (and the vlm entry's `precision`/`device`/`pipeline_type`) of each matching
entry by `type:` — it leaves every other authored field (hub, is_ovms,
embeddings/rerank device & precision, parallel_downloads, download_path)
untouched, since those aren't duplicated anywhere in config.yaml and have no
single source of truth to sync from. The base startup-models.yaml is kept
CPU-safe so the plain docker/docker-compose.yml path works on machines without
/dev/dri; startup-models.gpu.yaml carries the configured accelerator device.

Run standalone::

    python -m utils.sync_startup_models          # print would-be changes (dry run)
    python -m utils.sync_startup_models --write   # apply changes in place
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List

import yaml

from utils.config_loader import config

logger = logging.getLogger(__name__)

SC_ROOT = Path(__file__).resolve().parents[1]
STARTUP_MODELS_FILES = [
    SC_ROOT / "components/vlm/config/startup-models.yaml",
    SC_ROOT / "components/vlm/config/startup-models.gpu.yaml",
]


def _wanted_names() -> Dict[str, Dict[str, Any]]:
    """Map startup-models `type:` -> desired overrides, read from config.yaml."""
    text_gen = getattr(config.models, "text_gen", None)
    embedding = getattr(config.models, "embedding", None)
    content_search = getattr(config, "content_search", None)
    file_ingest = getattr(content_search, "file_ingest", None)
    reranker = getattr(file_ingest, "reranker", None)

    wanted: Dict[str, Dict[str, Any]] = {}
    if text_gen is not None and getattr(text_gen, "vlm_name", None):
        ovms = getattr(text_gen, "ovms", None)
        wanted["vlm"] = {
            "name": text_gen.vlm_name,
            "precision": getattr(text_gen, "weight_format", None),
            "device": getattr(text_gen, "device", None),
            "pipeline_type": getattr(ovms, "pipeline_type", "VLM"),
        }
    if embedding is not None and getattr(embedding, "name", None):
        wanted["embeddings"] = {"name": embedding.name}
    if reranker is not None and getattr(reranker, "model", None):
        wanted["rerank"] = {"name": reranker.model}
    return wanted


def _apply(
    data: Dict[str, Any],
    wanted: Dict[str, Dict[str, Any]],
    *,
    keep_vlm_cpu_safe: bool,
) -> List[str]:
    """Mutate `data["models"]` entries in place; return a list of change
    descriptions (empty if nothing changed)."""
    changes: List[str] = []
    for entry in data.get("models", []):
        overrides = wanted.get(entry.get("type"))
        if not overrides:
            continue
        for key, value in overrides.items():
            if value is None:
                continue
            target = entry if key == "name" else entry.setdefault("config", {})
            if key == "device":
                target = entry.setdefault("config", {})
                if keep_vlm_cpu_safe and entry.get("type") == "vlm":
                    value = "CPU"
            field = "name" if key == "name" else key
            old = target.get(field)
            if old != value:
                changes.append(
                    f"{entry.get('type')}.{field}: {old!r} -> {value!r}"
                )
                target[field] = value
    return changes


def sync(write: bool = False) -> bool:
    """Returns True if any file had (or would have) changes."""
    wanted = _wanted_names()
    any_changes = False
    for path in STARTUP_MODELS_FILES:
        if not path.is_file():
            logger.warning("⚠️  Missing %s — skipping.", path)
            continue
        text = path.read_text(encoding="utf-8")
        header = "\n".join(
            line for line in text.splitlines() if line.startswith("#")
        )
        data = yaml.safe_load(text) or {}
        changes = _apply(
            data,
            wanted,
            keep_vlm_cpu_safe=path.name == "startup-models.yaml",
        )
        if not changes:
            logger.info("✅ %s already in sync.", path.name)
            continue
        any_changes = True
        logger.info("%s %s:", "Updating" if write else "Would update", path.name)
        for change in changes:
            logger.info("  - %s", change)
        if write:
            body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
            path.write_text(f"{header}\n\n{body}" if header else body, encoding="utf-8")
    return any_changes


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Sync startup-models(.gpu).yaml model names from config.yaml."
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Apply changes in place instead of just reporting them.",
    )
    args = parser.parse_args()
    sync(write=args.write)


if __name__ == "__main__":
    main()

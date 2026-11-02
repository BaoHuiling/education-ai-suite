# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from utils.config_loader import config

logger = logging.getLogger(__name__)

SC_ROOT = Path(__file__).resolve().parents[1]
OVMS_MODELS_DIR = Path(os.environ.get("OVMS_MODELS_ROOT") or (SC_ROOT / "models" / "ovms"))
OVMS_CONFIG_PATH = OVMS_MODELS_DIR / "config.json"
STARTUP_MODELS_PATH = Path(
    os.environ.get("STARTUP_MODELS_CONFIG")
    or (SC_ROOT / "components" / "vlm" / "config" / "startup-models.yaml")
)

_STARTUP_TYPE_BY_KIND = {
    "text_generation": "vlm",
    "embeddings": "embeddings",
    "rerank": "rerank",
}

_DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"

# When set (docker compose sets it), provisioning first waits for model-downloader's
# startup jobs to finish. Its compose healthcheck only says the API is up, while the
# startup model exports keep running afterwards.
MODEL_DOWNLOADER_URL = os.environ.get("MODEL_DOWNLOADER_URL", "")
MODEL_DOWNLOADER_WAIT_TIMEOUT_S = float(os.environ.get("MODEL_DOWNLOADER_WAIT_TIMEOUT_S", "1800"))
_TERMINAL_JOB_STATES = {"completed", "failed", "canceled", "cancelled"}


def _fetch_downloader_jobs(url: str) -> list:
    # Internal service call: bypass any HTTP(S)_PROXY from the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{url.rstrip('/')}/jobs", timeout=10) as resp:
        return json.load(resp).get("jobs", [])


def wait_for_downloader_jobs(
    url: str = MODEL_DOWNLOADER_URL,
    timeout_s: float = MODEL_DOWNLOADER_WAIT_TIMEOUT_S,
    poll_s: float = 5.0,
) -> None:
    """Block until model-downloader has no in-flight jobs (no-op if ``url`` is unset)."""
    if not url:
        return
    deadline = time.monotonic() + timeout_s
    announced = False
    while True:
        try:
            jobs = _fetch_downloader_jobs(url)
        except (OSError, ValueError) as exc:
            logger.warning("⚠️  Could not query model-downloader at %s (%s) — relying on the on-disk check.", url, exc)
            return
        active = [j for j in jobs if str(j.get("status", "")).lower() not in _TERMINAL_JOB_STATES]
        if not active:
            failed = [j.get("model_name") for j in jobs if str(j.get("status", "")).lower() == "failed"]
            if failed:
                logger.warning("⚠️  model-downloader job(s) failed: %s", ", ".join(map(str, failed)))
            return
        if not announced:
            logger.info(
                "⏳ Waiting for model-downloader to finish: %s",
                ", ".join(str(j.get("model_name")) for j in active),
            )
            announced = True
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"model-downloader still has {len(active)} job(s) in progress after {timeout_s:.0f}s."
            )
        time.sleep(poll_s)


@dataclass
class Servable:
    """One OVMS servable to export into the model repository."""

    kind: str  # export_model.py task: text_generation | embeddings | rerank
    source_model: str  # HF/ModelScope repo id
    target_device: str  # CPU | GPU | NPU
    weight_format: Optional[str] = None  # int4 | int8 | None (no quantization)
    model_name: str = field(default="")  # served name; defaults to basename of source
    pipeline_type: Optional[str] = None  # LM | LM_CB | VLM | VLM_CB | AUTO
    cache_size_gb: Optional[int] = None  # KV cache size in GB (0/None = dynamic)
    max_num_seqs: Optional[int] = None  # max concurrent sequences OVMS will batch
    max_num_batched_tokens: Optional[int] = None
    enable_prefix_caching: Optional[bool] = None

    def __post_init__(self) -> None:
        if not self.model_name:
            self.model_name = self.source_model.split("/")[-1]

    @property
    def bucket_dir(self) -> Path:
        """The ``<device>/<precision>`` bucket model-downloader writes into."""
        precision = self.weight_format or "int8"
        return OVMS_MODELS_DIR / "openvino_models" / self.target_device.lower() / precision

    @property
    def model_dir(self) -> Path:
        # model-downloader keeps the full "<org>/<model>" source id as the
        # directory name (no basename-only flattening, no version subdir).
        return self.bucket_dir / self.source_model

    @property
    def graph_path(self) -> str:
        # Informational only — the real config.json is assembled by scanning
        # each bucket's own config_all.json (see write_config_json), not from
        # this path. Kept for logging/debugging.
        return f"{self.source_model}/graph.pbtxt"

    def is_ready(self) -> bool:
        """True when a usable IR (+ graph, for text_gen) already exists.

        Checks every ``<device>/<precision>`` bucket for this source_model,
        not just the one matching ``target_device`` — model-downloader's
        actual export device is driven by whichever ``startup-models*.yaml``
        the compose GPU overlay mounted, which can differ from what
        config.yaml says (e.g. embeddings/rerank default to CPU in
        config.yaml but the GPU overlay exports them to GPU). write_config_json()
        merges whatever is on disk regardless of device, so readiness checks
        must do the same to avoid false "missing" failures.
        """
        root = OVMS_MODELS_DIR / "openvino_models"
        for model_dir in root.glob(f"*/*/{self.source_model}"):
            if (model_dir / "graph.pbtxt").is_file() or any(model_dir.glob("*.xml")):
                return True
        return False


def _hub() -> str:
    """Return the configured model hub ('huggingface' | 'modelscope')."""
    return str(getattr(config.models, "model_hub", "huggingface")).lower()


def _text_gen_spec() -> Servable:
    tg = config.models.text_gen
    source = getattr(tg, "ovms_model", None) or getattr(tg, "vlm_name")
    ovms_cfg = getattr(tg, "ovms", None)
    return Servable(
        kind="text_generation",
        source_model=str(source),
        target_device=str(getattr(tg, "device", "GPU")).upper(),
        weight_format=str(getattr(tg, "weight_format", "int4")),
        pipeline_type=getattr(ovms_cfg, "pipeline_type", "VLM"),
        cache_size_gb=getattr(ovms_cfg, "cache_size_gb", None) if ovms_cfg else None,
        max_num_seqs=getattr(ovms_cfg, "max_num_seqs", None) if ovms_cfg else None,
        max_num_batched_tokens=getattr(ovms_cfg, "max_num_batched_tokens", None) if ovms_cfg else None,
        enable_prefix_caching=getattr(ovms_cfg, "enable_prefix_caching", True) if ovms_cfg else True,
    )


def _embeddings_spec() -> Servable:
    emb = getattr(config.models, "embedding", None)
    source = getattr(emb, "ovms_model", None) or getattr(emb, "name", None) \
        or "BAAI/bge-large-en-v1.5"
    return Servable(
        kind="embeddings",
        source_model=str(source),
        target_device=str(getattr(emb, "device", "CPU")).upper() if emb else "CPU",
    )


def _rerank_spec() -> Servable:
    content_search = getattr(config, "content_search", None)
    file_ingest = getattr(content_search, "file_ingest", None) if content_search else None
    reranker = getattr(file_ingest, "reranker", None) if file_ingest else None
    rr = getattr(config.models, "rerank", None)
    source = (
        getattr(rr, "ovms_model", None)
        or getattr(rr, "name", None)
        or getattr(reranker, "model", None)
        or _DEFAULT_RERANK_MODEL
    )
    device = getattr(rr, "device", None) or getattr(reranker, "device", None) or "CPU"
    return Servable(
        kind="rerank",
        source_model=str(source),
        target_device=str(device).upper(),
    )


def build_specs() -> List[Servable]:
    """Assemble the three servable specs from config (with sane fallbacks)."""
    return [_text_gen_spec(), _embeddings_spec(), _rerank_spec()]


def _discover_bucket_configs() -> List[Path]:
    """Find every ``config_all.json`` model-downloader wrote, one per bucket."""
    bucket_root = OVMS_MODELS_DIR / "openvino_models"
    return sorted(bucket_root.glob("*/*/config_all.json"))


def _startup_model_selection() -> dict[str, tuple[str, str]]:
    """Return the expected ``name -> (device, precision)`` from startup-models."""
    if not STARTUP_MODELS_PATH.is_file():
        logger.warning("⚠️  Missing %s — merging all bucket configs.", STARTUP_MODELS_PATH)
        return {}

    data = yaml.safe_load(STARTUP_MODELS_PATH.read_text(encoding="utf-8")) or {}
    selection: dict[str, tuple[str, str]] = {}
    for model in data.get("models", []):
        cfg = model.get("config", {}) or {}
        name = model.get("name")
        if not name:
            continue
        selection[str(name)] = (
            str(cfg.get("device", "CPU")).lower(),
            str(cfg.get("precision", "int8")).lower(),
        )
    return selection


def write_config_json(specs: Optional[List[Servable]] = None) -> Path:
    """Merge every bucket's ``config_all.json`` (written by model-downloader)
    into the single ``models/ovms/config.json`` the ``ovms`` service reads.

    ``specs`` is accepted for API back-compat but unused — the merge is driven
    entirely by what's actually on disk, since that's what model-downloader
    (not this module) exports.
    """
    bucket_configs = _discover_bucket_configs()
    if not bucket_configs:
        raise RuntimeError(
            f"No config_all.json found under {OVMS_MODELS_DIR / 'openvino_models'}. "
            f"Run the model-downloader container first (docker compose up model-downloader)."
        )

    selected_buckets = _startup_model_selection()
    merged_models: list = []
    names: list = []
    seen_names: set[str] = set()
    for bucket_cfg in bucket_configs:
        rel_bucket = bucket_cfg.parent.relative_to(OVMS_MODELS_DIR)
        bucket_device = bucket_cfg.parent.parent.name.lower()
        bucket_precision = bucket_cfg.parent.name.lower()
        bucket_doc = json.loads(bucket_cfg.read_text(encoding="utf-8"))
        for entry in bucket_doc.get("model_config_list", []):
            cfg = dict(entry.get("config", {}))
            name = cfg.get("name")
            if not name:
                continue
            expected_bucket = selected_buckets.get(str(name))
            if expected_bucket and expected_bucket != (bucket_device, bucket_precision):
                continue
            if name in seen_names:
                continue
            base_path = cfg.get("base_path")
            if base_path:
                cfg["base_path"] = str(rel_bucket / base_path)
            merged_models.append({"config": cfg})
            names.append(name)
            seen_names.add(str(name))

    doc = {"model_config_list": merged_models}
    OVMS_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    OVMS_CONFIG_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "✅ Wrote %s (%d servables merged from %d bucket config(s): %s)",
        OVMS_CONFIG_PATH,
        len(merged_models),
        len(bucket_configs),
        ", ".join(n for n in names if n),
    )
    return OVMS_CONFIG_PATH


def write_startup_config(specs: List[Servable], path: Path = STARTUP_MODELS_PATH) -> Path:
    """Generate ``components/vlm/config/startup-models.yaml`` from the servable specs.

    Consumed by the vendored ``model-downloader`` container
    (``STARTUP_MODELS_CONFIG``) to provision IR for all three servables before
    ``ovms`` starts. Regenerating this from ``build_specs()`` keeps it in sync
    with ``config.yaml`` instead of risking manual drift between the two.
    """
    doc = {
        "download_path": "ovms/",
        "parallel_downloads": True,
        "models": [
            {
                "name": s.source_model,
                "hub": "openvino",
                "type": _STARTUP_TYPE_BY_KIND.get(s.kind, s.kind),
                "is_ovms": True,
                "config": {
                    "precision": s.weight_format or "int8",
                    "device": s.target_device,
                    **({"pipeline_type": s.pipeline_type} if s.pipeline_type is not None else {}),
                },
            }
            for s in specs
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Copyright (C) 2026 Intel Corporation\n"
        "# SPDX-License-Identifier: Apache-2.0\n"
        "#\n"
        "# Generated by `python -m utils.ovms_provision --write-startup-config` from\n"
        "# utils/ovms_provision.py::build_specs() — do not hand-edit; edit config.yaml's\n"
        "# models.{text_gen,embedding,rerank} block and regenerate instead.\n\n"
    )
    path.write_text(header + yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    logger.info(
        "✅ Wrote %s (%d models: %s)",
        path,
        len(specs),
        ", ".join(s.source_model for s in specs),
    )
    return path


def provision_ovms_models(specs: Optional[List[Servable]] = None, check: bool = False) -> Path:
    """(Re)write ``models/ovms/config.json`` from the current servable specs.

    IR/graph export itself happens in the ``model-downloader`` container
    (see ``components/vlm/config/startup-models.yaml``) before ``ovms`` starts — this only
    assembles the mediapipe config that points at each servable's graph.
    """
    specs = specs or build_specs()
    wait_for_downloader_jobs()

    if check:
        missing = [s.model_name for s in specs if not s.is_ready()]
        if missing:
            raise RuntimeError(
                f"Missing IR/graph for servable(s): {', '.join(missing)}. "
                f"Run the model-downloader container to provision them "
                f"(docker compose up model-downloader) before starting OVMS."
            )

    write_config_json(specs)
    logger.info("🎉 OVMS model repository config ready at %s", OVMS_CONFIG_PATH)
    return OVMS_MODELS_DIR


def _selected_specs(args: argparse.Namespace) -> List[Servable]:
    all_specs = {"text_gen": _text_gen_spec, "embeddings": _embeddings_spec, "rerank": _rerank_spec}
    chosen = [k for k in all_specs if getattr(args, k)]
    if not chosen:  # none flagged => all
        chosen = list(all_specs)
    return [all_specs[k]() for k in chosen]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="(Re)write the OVMS model repository config.json (0.C.1)."
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Verify IR/graph exist for each servable before writing config.json.",
    )
    parser.add_argument(
        "--write-startup-config", action="store_true",
        help="(Re)generate components/vlm/config/startup-models.yaml from build_specs() instead of "
             "writing config.json.",
    )
    parser.add_argument("--text-gen", dest="text_gen", action="store_true", help="Select only text_gen.")
    parser.add_argument("--embeddings", action="store_true", help="Select only embeddings.")
    parser.add_argument("--rerank", action="store_true", help="Select only rerank.")
    args = parser.parse_args()

    specs = _selected_specs(args)
    if args.write_startup_config:
        write_startup_config(specs)
        return

    provision_ovms_models(specs=specs, check=args.check)


if __name__ == "__main__":
    main()

# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Which model hub downloads go to, decided once from ``models.model_hub``.

The setting only redirects models whose files are identical on both hubs
(OpenVINO IR repos, pyannote, FunASR VAD/CAM++, PaddleOCR, grading layout).
The rest keep their one source whatever it says: FunASR Paraformer and
punctuation on ModelScope; Whisper exports, other optimum exports and the
speculative-decoding drafters (pinned to Hugging Face commits) on Hugging Face.
"""

import logging
import os

logger = logging.getLogger(__name__)

HF = "hf"
MS = "ms"
HUB_LABELS = {HF: "Hugging Face", MS: "ModelScope"}
CONFIG_VALUES = {HF: "huggingface", MS: "modelscope"}

_ALIASES = {
    "hf": HF,
    "huggingface": HF,
    "hugging_face": HF,
    "ms": MS,
    "modelscope": MS,
}

# PaddleOCR (paddlex) reads its download source from this variable at import.
_PADDLE_SOURCE = {HF: "huggingface", MS: "modelscope"}


def normalize_hub(value) -> str:
    """Map a config spelling to ``"hf"`` or ``"ms"``; blank means Hugging Face."""
    if value is None or str(value).strip() == "":
        return HF
    hub = _ALIASES.get(str(value).strip().lower())
    if hub is None:
        raise ValueError(
            f"Unsupported models.model_hub '{value}': use huggingface or modelscope."
        )
    return hub


def get_model_hub() -> str:
    from utils.config_loader import config

    return normalize_hub(getattr(config.models, "model_hub", None))


def warn_if_not_configured_hub(model: str, actual_hub: str) -> None:
    """Say so when a download goes to a hub other than ``models.model_hub``.

    Call only when a download actually starts, so cached models stay quiet.
    """
    configured = get_model_hub()
    if configured != actual_hub:
        logger.warning(
            f"'{model}' is downloaded from {HUB_LABELS[actual_hub]} only; "
            f"models.model_hub: {CONFIG_VALUES[configured]} does not apply to it."
        )


def apply_hub_env(models_cfg) -> None:
    """Export the hub choice to libraries that only read environment variables.

    Must run before ``huggingface_hub`` and ``paddlex`` are imported: both read
    these variables once, at import. Values already set in the environment win.
    """
    models_cfg = models_cfg if isinstance(models_cfg, dict) else {}
    hub = normalize_hub(models_cfg.get("model_hub"))
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", _PADDLE_SOURCE[hub])
    endpoint = str(models_cfg.get("hf_endpoint") or "").strip().rstrip("/")
    if endpoint:
        os.environ.setdefault("HF_ENDPOINT", endpoint)

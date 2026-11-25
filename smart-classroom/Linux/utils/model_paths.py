# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Where the converted OpenVINO IRs live."""

from pathlib import Path
from typing import Optional

SC_ROOT = Path(__file__).resolve().parents[1]
OPENVINO_MODELS_DIR = SC_ROOT / "models" / "openvino"


def openvino_model_dir(model_name: str, weight_format: str) -> Path:
    """Return the shared IR directory ``models/openvino/<name>/<weight_format>``."""
    return OPENVINO_MODELS_DIR / model_name.split("/")[-1] / weight_format.lower()


def text_gen_model_dir() -> Optional[Path]:
    """Return the IR directory of the configured ``models.text_gen`` VLM.

    With ``provider: ovms`` the IR lives in model-downloader's OVMS layout
    (``models/ovms/openvino_models/<device>/<precision>/<org>/<model>``), not in
    the in-process ``models/openvino`` tree.
    """
    from utils.config_loader import config

    text_gen = getattr(config.models, "text_gen", None)
    name = getattr(text_gen, "vlm_name", None)
    weight = getattr(text_gen, "weight_format", None)
    if not name or not weight:
        return None
    if str(getattr(text_gen, "provider", "") or "").lower() == "ovms":
        from utils.ovms_provision import text_gen_servable_dir

        return text_gen_servable_dir()
    return openvino_model_dir(str(name), str(weight))

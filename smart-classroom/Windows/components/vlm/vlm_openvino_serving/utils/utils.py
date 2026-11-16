# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import base64
import os
import random
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Dict, List

import numpy as np
import openvino as ov
import torch
import yaml
from PIL import Image
from providers.utils.model_utils import is_model_ready
from .common import ErrorMessages, logger

__all__ = ["convert_model", "is_model_ready", "load_images", "load_model_config", "setup_seed"]


def _preconverted_repo(model_id: str, weight_format: str):
    """Pre-converted IR repository from the text_gen catalog, if one exists."""
    from components.vlm.text_gen_profiles import preconverted_repo

    return preconverted_repo(model_id, weight_format)


def _download_preconverted_ov_model(repo_id: str, cache_dir: str):
    # Backend only (model_type "vlm"), where models.model_hub is loaded. The
    # OpenVINO org publishes identical IR on Hugging Face and ModelScope.
    from utils.model_download_helper import download_snapshot
    from utils.model_hub import get_model_hub

    logger.info(f"Downloading pre-converted OpenVINO IR '{repo_id}' -> {cache_dir}")
    download_snapshot(
        get_model_hub(),
        repo_id,
        cache_dir,
        allow_patterns=[
            "openvino_*.xml",
            "openvino_*.bin",
            "*.json",
            "*.jinja",
            "*.txt",
        ],
    )
    logger.info("Pre-converted OpenVINO IR download complete.")


_CONVERT_WORKER = Path(__file__).resolve().parent / "convert_worker.py"

# ---------------------------------------------------------------------------
# Export-time transformers pin
# ---------------------------------------------------------------------------
# optimum-intel 2.1.0's per-architecture support table caps Qwen3_5 / 
# Qwen3_5Moe / Qwen3_5Text / Qwen3_5MoeText at transformers 5.2.0. 
# Above 5.2.0 the OpenVINO export patcher fails with "cannot import name
# 'Qwen3_5DynamicCache' from transformers.models.qwen3_5.modeling_qwen3_5"
# (optimum-intel issue #1786). Installed with --no-deps into a side directory
# used only by the export subprocess, so requirements.txt keeps the newer pin:
# 5.2.0 needs huggingface-hub>=1.3.0, tokenizers>=0.22.0,<=0.23.0,
# safetensors>=0.4.3 and typer-slim, all satisfied by requirements.txt.
_EXPORT_TRANSFORMERS_VERSION = "5.2.0"
_EXPORT_TRANSFORMERS_MODEL_MARKERS = ("qwen3.5", "qwen3.6")
_SC_ROOT = Path(__file__).resolve().parents[4]


def _models_base() -> Path:
    ud = os.environ.get("SMART_CLASSROOM_USERDATA")
    return Path(ud) / "models" if ud and ud.strip() else (_SC_ROOT / "models")


def _export_overlay_dir(version: str = _EXPORT_TRANSFORMERS_VERSION) -> Path:
    """Directory holding an export-only transformers build.

    Lives under the gitignored ``models/`` tree. ``SC_EXPORT_DEPS_DIR`` overrides
    the default version's directory so a setup script can provision the overlay
    ahead of time (e.g. for an offline install).
    """
    override = os.environ.get("SC_EXPORT_DEPS_DIR")
    if override and version == _EXPORT_TRANSFORMERS_VERSION:
        return Path(override)
    return _models_base() / ".export-deps" / f"transformers-{version}"


def _needs_transformers_overlay(model_id: str) -> bool:
    name = str(model_id).lower()
    return any(marker in name for marker in _EXPORT_TRANSFORMERS_MODEL_MARKERS)


def ensure_transformers_overlay(version: str = _EXPORT_TRANSFORMERS_VERSION,
                                extra_specs: tuple = ()) -> Path:
    """Return the overlay dir, installing the pinned packages with --no-deps if missing.

    ``extra_specs`` (``"name==version"``) are installed beside transformers, for a
    build whose own requirements differ from the main venv's.
    """
    overlay = _export_overlay_dir(version)
    specs = [f"transformers=={version}", *extra_specs]
    markers = [overlay / spec.split("==")[0].replace("-", "_") / "__init__.py" for spec in specs]
    if all(marker.exists() for marker in markers):
        logger.info(f"Using export-only transformers overlay at {overlay}")
        return overlay

    logger.info(f"Provisioning {' '.join(specs)} for the export subprocess in {overlay}...")
    overlay.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--upgrade",
         "--target", str(overlay), *specs]
    )
    if completed.returncode != 0 or not all(marker.exists() for marker in markers):
        raise RuntimeError(
            f"Could not provision {' '.join(specs)}, required to export this model. "
            f"Install it manually and retry:\n"
            f'  python -m pip install --no-deps --target "{overlay}" {" ".join(specs)}'
        )
    return overlay



def convert_model(
    model_id: str, cache_dir: str, model_type: str = "vlm", weight_format: str = "int4"
):
    """
    Converts a specified model to OpenVINO format and saves it to the cache directory.

    The conversion runs in a subprocess so that all memory used during quantization
    and export is fully released when the subprocess exits.

    Args:
        model_id (str): The identifier of the model to be converted.
        cache_dir (str): The directory where the converted model will be saved.
        model_type (str): The type of the model. It can be "embedding", "reranker", "llm", or "vlm".
        weight_format (str): The format of the model weights. Used for specific model types like "llm" and "vlm".
    Returns:
        None

    Raises:
        ValueError: If the model_type is not one of "embedding", "reranker", "llm", or "vlm".
        RuntimeError: If the subprocess fails during conversion.
    """
    try:
        logger.debug(f"cache_ddir: {cache_dir}")
        require_detokenizer = model_type in ("llm", "vlm")
        preconverted_repo = _preconverted_repo(model_id, weight_format) if model_type == "vlm" else None
        if is_model_ready(Path(cache_dir), require_detokenizer=require_detokenizer):
            logger.info(f"Optimized {model_id} exist in {cache_dir}. Skip process...")
        elif preconverted_repo is not None:
            _download_preconverted_ov_model(preconverted_repo, cache_dir)
        else:
            logger.info(f"Converting {model_id} model to OpenVINO format in subprocess...")
            # Run a standalone script rather than multiprocessing.Process: on
            # Windows the spawn start method re-executes the parent's __main__
            # (main.py) in the child, which needlessly re-imports the whole app
            # and breaks on top-level package-name collisions in sys.path.
            env = os.environ.copy()
            search_path = [p for p in sys.path if p]
            if _needs_transformers_overlay(model_id):
                # Must come first: PYTHONPATH entries are searched in order.
                search_path.insert(0, str(ensure_transformers_overlay()))
            env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(search_path))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(_CONVERT_WORKER),
                    "--model-id", model_id,
                    "--cache-dir", cache_dir,
                    "--model-type", model_type,
                    "--weight-format", weight_format,
                ],
                env=env,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Model conversion subprocess failed with exit code {completed.returncode}"
                )
            logger.info(f"Model conversion completed. Subprocess memory released.")
    except Exception as e:
        logger.error(f"Error occurred during model conversion: {e}")
        raise RuntimeError(f"Error occurred during model conversion: {e}")


async def load_images(image_urls_or_files: List[str]):
    """
    Load images from base64 data URLs or local file paths.

    Args:
        image_urls_or_files (List[str]): A list of base64 data URLs or file paths.

    Returns:
        Tuple[List[Image.Image], List[ov.Tensor]]: PIL images and OpenVINO tensors.
    """
    images = []
    image_tensors = []
    for source in image_urls_or_files:
        try:
            if str(source).startswith("data:image/"):
                # base64 data URL: data:image/<mime>;base64,<data>
                decoded = base64.b64decode(source.split(",", 1)[1])
                image = Image.open(BytesIO(decoded)).convert("RGB")
            else:
                image = Image.open(source).convert("RGB")
            # NHWC uint8 straight from the decoded buffer; getdata() walks
            # every pixel through Python tuples (60-90 ms for a 640x480 frame).
            image_data = np.ascontiguousarray(np.asarray(image, dtype=np.uint8)[None])
            images.append(image)
            image_tensors.append(ov.Tensor(image_data))
        except base64.binascii.Error as e:
            raise ValueError(f"Invalid base64 image data: {e}")
        except Exception as e:
            raise RuntimeError(f"{ErrorMessages.LOAD_IMAGE_ERROR}: {e}")
    return images, image_tensors


def load_model_config(
    model_name: str, config_path: Path = Path(__file__).resolve().parent.parent / "config" / "model_config.yaml"
) -> Dict:
    """
    Load the configuration for a specific model from a YAML file.

    Args:
        model_name (str): The name of the model.
        config_path (Path): Path to the configuration file.

    Returns:
        dict: The configuration for the specified model.

    Raises:
        RuntimeError: If an error occurs while loading or parsing the configuration.
    """
    try:
        with open(config_path, "r") as config_file:
            configs = yaml.safe_load(config_file)
        config = configs.get(model_name.lower(), {})
        logger.info(f"Loaded configuration for model '{model_name}': {config}")
        return config
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        return {}
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML configuration: {e}")
        raise RuntimeError(f"Error parsing YAML configuration: {e}")
    except Exception as e:
        logger.error(f"Error loading model configuration: {e}")
        raise RuntimeError(f"Error loading model configuration: {e}")


def setup_seed(seed: int):
    """
    Set up the random seed for reproducibility.

    Args:
        seed (int): The seed value to use.
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    logger.info(f"Random seed set to: {seed}")




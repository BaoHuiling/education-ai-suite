import yaml
from types import SimpleNamespace
import os
import logging

logger = logging.getLogger(__name__)

# Canonical feature ids recognized in the `features:` config block.
KNOWN_FEATURE_IDS = frozenset({
    "asr",
    "summary",
    "mindmap",
    "topic_segmentation",
    "video_analytics",
    "board_ocr",
    "content_search",
    "qa",
    "grading",
    "report",
})

def _dict_to_namespace(d):
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _dict_to_namespace(v) for k, v in d.items()})
    return d

def _apply_backcompat(data):
    """Normalize legacy config keys to the current structure in place."""
    if not isinstance(data, dict):
        return data
    models = data.get("models")
    return data

def _validate_features(data):
    """Reject unknown feature ids. An absent block enables all features."""
    if not isinstance(data, dict):
        return
    features = data.get("features")
    if features is None:
        return  # absent => all features enabled downstream
    if not isinstance(features, dict):
        raise ValueError(
            "'features:' config block must be a mapping of feature id -> settings."
        )
    unknown = set(features) - KNOWN_FEATURE_IDS
    if unknown:
        raise ValueError(
            "Unknown feature id(s) in 'features:' config block: "
            + ", ".join(sorted(unknown))
            + ". Known features: "
            + ", ".join(sorted(KNOWN_FEATURE_IDS))
            + "."
        )

def _normalize_va_device(data):
    """Normalize the VA device at startup, falling back to NPU when invalid."""
    if not isinstance(data, dict):
        return
    models = data.get("models")
    if not isinstance(models, dict):
        return
    va = models.get("va")
    if not isinstance(va, dict):
        return
    configured_value = va.get("device", "NPU")
    device = str(configured_value).upper()
    supported_devices = ("GPU", "NPU", "CPU")
    if device not in supported_devices:
        logger.warning(
            "Invalid VA device %r; falling back to NPU. Valid values are: %s",
            configured_value,
            ", ".join(supported_devices),
        )
        device = "NPU"
    va["device"] = device


def load_config(path="config.yaml"):
    # Allow override via environment variable SC_CONFIG_PATH
    # Used by Flutter integration to use utils/flutter/config.yaml
    env_config_path = os.environ.get("SC_CONFIG_PATH")
    if env_config_path:
        path = env_config_path
        logger.info(f"Using config from SC_CONFIG_PATH: {path}")
    
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data = _apply_backcompat(data)
    _validate_features(data)
    _normalize_va_device(data)
    # Imported before huggingface_hub/paddlex everywhere, so their env-driven
    # download settings are still unread here.
    from utils.model_hub import apply_hub_env
    apply_hub_env((data or {}).get("models"))
    return _dict_to_namespace(data)

config = load_config()

logger.debug("\n📦 CONFIGURATION START\n" + "-" * 40)
logger.debug(yaml.dump(vars(config), sort_keys=False))
logger.debug("\n" + "-" * 40 + "\n📦 CONFIGURATION END\n")

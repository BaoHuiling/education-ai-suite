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
    "content_search",
    "qa",
    "grading",
})


KNOWN_TEXT_GEN_PROVIDERS = frozenset({"in-process", "ovms"})

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

def _validate_text_gen(data):
    if not isinstance(data, dict):
        return
    models = data.get("models")
    if not isinstance(models, dict):
        return
    text_gen = models.get("text_gen")
    if not isinstance(text_gen, dict):
        return

    provider = text_gen.get("provider", "in-process")
    if provider not in KNOWN_TEXT_GEN_PROVIDERS:
        raise ValueError(
            f"Unknown models.text_gen.provider {provider!r}. "
            f"Known providers: {', '.join(sorted(KNOWN_TEXT_GEN_PROVIDERS))}."
        )

    if provider != "ovms":
        return

    ovms = text_gen.get("ovms")
    if not isinstance(ovms, dict):
        raise ValueError(
            "models.text_gen.ovms: block is required when "
            "models.text_gen.provider: ovms."
        )

    required = ("endpoint", "api_path", "timeout_s")
    missing = [key for key in required if not ovms.get(key)]
    if missing:
        raise ValueError(
            "models.text_gen.ovms: missing required field(s): "
            + ", ".join(missing)
            + " (required when provider: ovms)."
        )

    timeout_s = ovms.get("timeout_s")
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
        raise ValueError(
            "models.text_gen.ovms.timeout_s must be a positive number, "
            f"got {timeout_s!r}."
        )

    pipeline_type = ovms.get("pipeline_type", "VLM")
    if not isinstance(pipeline_type, str) or pipeline_type not in {"LM", "LM_CB", "VLM", "VLM_CB", "AUTO"}:
        raise ValueError(
            "models.text_gen.ovms.pipeline_type must be one of "
            f"LM, LM_CB, VLM, VLM_CB, AUTO; got {pipeline_type!r}."
        )

    for key in ("max_num_seqs", "max_num_batched_tokens", "cache_size_gb"):
        value = ovms.get(key)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError(
                f"models.text_gen.ovms.{key} must be a number or null, got {value!r}."
            )


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
    _validate_text_gen(data)
    return _dict_to_namespace(data)

config = load_config()

logger.debug("\n📦 CONFIGURATION START\n" + "-" * 40)
logger.debug(yaml.dump(vars(config), sort_keys=False))
logger.debug("\n" + "-" * 40 + "\n📦 CONFIGURATION END\n")


def resolve_ovms_base_url() -> str:
    """Resolve the OVMS base URL for the ``text_gen`` OVMS backend (Phase 4).

    Precedence: ``SERVING_BASE_URL`` env (set by docker-compose's ``app``
    service, see ``.env.example``) → ``config.models.text_gen.ovms.endpoint``
    → localhost default — mirrors ``audio_analyzer_client._resolve_base_url()``.
    """
    env_url = os.environ.get("SERVING_BASE_URL")
    if env_url:
        return env_url.rstrip("/")

    text_gen = getattr(getattr(config, "models", None), "text_gen", None)
    ovms = getattr(text_gen, "ovms", None) if text_gen is not None else None
    endpoint = getattr(ovms, "endpoint", None) if ovms is not None else None
    if endpoint:
        return str(endpoint).rstrip("/")

    return "http://127.0.0.1:9000"

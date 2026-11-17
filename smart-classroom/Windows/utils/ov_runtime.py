# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Shared OpenVINO GPU runtime configuration for Smart Classroom services."""

import logging

_VALID_PRIORITIES = ("LOW", "MEDIUM", "HIGH")
_DEFAULT_PRIORITY = "LOW"
logger = logging.getLogger(__name__)


def gpu_queue_priority() -> str:
    """Read the configured GPU queue priority, defaulting to LOW."""
    try:
        from utils.config_loader import config

        models = getattr(config, "models", None)
        configured_value = getattr(models, "gpu_queue_priority", _DEFAULT_PRIORITY)
        value = str(configured_value or _DEFAULT_PRIORITY).upper()
    except Exception:
        value = _DEFAULT_PRIORITY
    if value not in _VALID_PRIORITIES:
        logger.warning(
            "Invalid GPU queue priority %r; falling back to LOW. Valid values are: %s",
            configured_value,
            ", ".join(_VALID_PRIORITIES),
        )
        return _DEFAULT_PRIORITY
    return value


def gpu_plugin_config(device, base=None) -> dict:
    """Merge GPU queue priority into plugin options only for GPU devices."""
    config = dict(base) if base else {}
    if str(device).upper().startswith("GPU"):
        config["GPU_QUEUE_PRIORITY"] = gpu_queue_priority()
    return config
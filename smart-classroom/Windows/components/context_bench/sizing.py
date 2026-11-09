# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""IR introspection and scheduler cache-pool estimation (re-exported by `benchmark.py`).

Everything here reads local files only (config.json, IR XML) -- no OpenVINO import. The
estimate is a preflight check, not capacity certification: passing it does not establish
measured memory-budget compliance.

Estimated demand, in bytes:

1. growing KV per token = ``2 * full_attention_layers * kv_heads * (head_dim * element_bytes
   + quantization_overhead_per_row)``, from top-level or nested ``text_config``; an
   inconsistent ``layer_types`` list is unknown, never an underestimate;
2. times the token count: context + decode ceiling (+ k + 1 verification tokens for
   speculative profiles); context 0 has no known prompt length at preflight, so its
   estimate is unavailable rather than zero;
3. plus fixed linear-attention state (unique IR state variables) times the reserved
   sequence / linear-attention rows;
4. plus the speculative (DFlash or Eagle3) draft's KV: every draft layer, its sliding-window
   layers capped at the declared window (`draft_kv_bytes`).

Any unknown contribution makes the whole estimate unavailable.
"""

from __future__ import annotations

import json
import math
import os
import re
import xml.etree.ElementTree as ET

from components.context_bench import trial_runner

_KV_PRECISION_BYTES = {
    "f32": 4, "f16": 2, "bf16": 2, "u8": 1, "i8": 1, "u4": 0.5, "i4": 0.5,
    "f8e4m3": 1, "f8e5m2": 1,
}
# "dynamic" (the GPU default) means the plugin decides, in practice f16.
_DEFAULT_KV_BYTES = 2
_COMPRESSED_KV_PRECISIONS = {"u8", "i8", "u4", "i4"}
# Scale and zero-point per compressed row.
_KV_QUANT_PARAMS_BYTES = 4
_IR_TYPE_BYTES = {"f32": 4, "fp32": 4, "f16": 2, "fp16": 2, "bf16": 2, "i8": 1, "u8": 1}

AUTO_CACHE_SAFETY = 1.3
AUTO_CACHE_RESERVE_GB = 8.0
AUTO_CACHE_MIN_GB = 2.0

GENAI_DEFAULT_MAX_NUM_SEQS = 256

_IR_TAIL_BYTES = 64 * 1024
_GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Artifact reads
# ---------------------------------------------------------------------------
def weight_disk_gb(model_dir) -> float:
    """Weight footprint from the IR .bin files, GiB."""
    total = 0
    for root, _, files in os.walk(str(model_dir)):
        for name in files:
            if name.endswith(".bin"):
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return total / _GIB


def ir_weight_precision(model_dir, names: tuple = trial_runner.LANGUAGE_MODEL_IRS) -> str | None:
    """The weight compression an IR actually carries, from its NNCF rt_info.

    "uncompressed" when the IR has no NNCF block; None when there is no IR to read. Only
    the XML's tail is read, never the weights.
    """
    path = next(
        (os.path.join(str(model_dir), name) for name in names
         if model_dir and os.path.isfile(os.path.join(str(model_dir), name))),
        None,
    )
    if path is None:
        return None
    try:
        with open(path, "rb") as ir_file:
            ir_file.seek(0, os.SEEK_END)
            ir_file.seek(max(0, ir_file.tell() - _IR_TAIL_BYTES))
            tail = ir_file.read().decode("utf-8", "replace")
    except OSError:
        return None
    block = re.search(r"<weight_compression>(.*?)</weight_compression>", tail, re.S)
    if block is None:
        return "uncompressed"
    fields = dict(re.findall(r'<(\w+) value="([^"]*)"', block.group(1)))
    mode = fields.get("mode", "?")
    group = fields.get("group_size")
    parts = [mode, "per-channel" if group in (None, "-1") else f"g{group}"]
    try:
        ratio = float(fields.get("ratio", "1"))
    except ValueError:
        ratio = 1.0
    if ratio < 1.0:
        parts.append(f"{ratio:.0%} of layers")
    parts += [name.upper() for name in ("awq", "gptq") if fields.get(name) == "True"]
    if fields.get("scale_estimation") == "True":
        parts.append("SE")
    if fields.get("all_layers") == "True":
        parts.append("all layers")
    elif fields.get("backup_mode") and fields["backup_mode"] != mode:
        parts.append(f"backup {fields['backup_mode']}")
    return " ".join(parts)


def precision_matches_label(precision: str | None, label: str | None) -> bool:
    """Whether a detected precision agrees with the `weight_format` label. Only integer
    labels are checked; a float label or an unreadable IR is not evidence of a mismatch."""
    if not precision or not label or not label.lower().startswith("int"):
        return True
    return precision.split()[0].lower().startswith(label.lower())


def load_model_config(model_dir) -> dict | None:
    try:
        with open(os.path.join(str(model_dir), "config.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def text_config(config: dict | None) -> dict:
    if not isinstance(config, dict):
        return {}
    nested = config.get("text_config")
    return nested if isinstance(nested, dict) and nested else config


def is_hybrid(config: dict | None) -> bool:
    layer_types = text_config(config).get("layer_types")
    return isinstance(layer_types, (list, tuple)) and any(
        t != "full_attention" for t in layer_types
    )


def fixed_state_cache_bytes(model_dir) -> int | None:
    """Fixed linear-attention state size from the IR's unique conv/ssm state variables.

    0 when the IR declares none; None when the IR is missing or unreadable.
    """
    model_xml = trial_runner.language_model_ir(str(model_dir))
    if model_xml is None:
        return None
    total = 0
    seen = set()
    try:
        with open(model_xml, "rb") as handle:
            states = []
            stack = []
            for event, elem in ET.iterparse(handle, events=("start", "end")):
                if event == "start":
                    stack.append(elem)
                else:
                    if "cache_params.past." in elem.attrib.get("variable_id", ""):
                        states.append(dict(elem.attrib))
                    stack.pop()
                    if stack:
                        stack[-1].remove(elem)
                    elem.clear()
        for attrib in states:
            variable_id = attrib.get("variable_id", "")
            if not re.search(r"cache_params\.past\.(?:conv|ssm)\.", variable_id):
                continue
            if variable_id in seen:
                continue
            seen.add(variable_id)
            item_bytes = _IR_TYPE_BYTES.get(attrib.get("variable_type", "").lower())
            shape = attrib.get("variable_shape", "")
            if not item_bytes or not shape:
                return None
            elements = 1
            for value in shape.split(","):
                value = value.strip()
                # The dynamic leading axis is the batch: one row per reserved sequence.
                elements *= 1 if value in ("?", "-1") else int(value)
            total += elements * item_bytes
    except (ET.ParseError, OSError, ValueError):
        return None
    return total


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------
def kv_cache_dtype_bytes(ov_config) -> float:
    precision = str((ov_config or {}).get("KV_CACHE_PRECISION", "")).strip().lower()
    return _KV_PRECISION_BYTES.get(precision, _DEFAULT_KV_BYTES)


def kv_quantization_param_bytes(ov_config) -> int:
    precision = str((ov_config or {}).get("KV_CACHE_PRECISION", "")).strip().lower()
    return _KV_QUANT_PARAMS_BYTES if precision in _COMPRESSED_KV_PRECISIONS else 0


def known_kv_precision(ov_config) -> bool:
    precision = str((ov_config or {}).get("KV_CACHE_PRECISION", "")).strip().lower()
    return precision in _KV_PRECISION_BYTES or precision in ("", "dynamic")


def theoretical_kv_bytes_per_token(config: dict, kv_cache_dtype_bytes: float = 2,
                                   quantization_param_bytes: int = 0) -> float | None:
    """Growing KV bytes per token: only ``full_attention`` layers grow with context.

    None when required architecture keys are missing or ``layer_types`` is inconsistent.
    """
    cfg = text_config(config)
    num_layers = cfg.get("num_hidden_layers")
    num_kv_heads = cfg.get("num_key_value_heads")
    head_dim = cfg.get("head_dim")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0
               for v in (num_layers, num_kv_heads, head_dim)):
        return None
    layer_types = cfg.get("layer_types")
    if layer_types is not None and (
        not isinstance(layer_types, (list, tuple)) or len(layer_types) != num_layers
    ):
        return None
    growing_layers = (
        sum(1 for t in layer_types if t == "full_attention") if layer_types else num_layers
    )
    if not growing_layers:
        return None
    bytes_per_row = head_dim * kv_cache_dtype_bytes + quantization_param_bytes
    return 2 * growing_layers * num_kv_heads * bytes_per_row


def draft_kv_bytes(draft_config: dict | None, tokens: int, kv_cache_dtype_bytes: float = 2,
                   quantization_param_bytes: int = 0) -> float | None:
    """KV bytes of a speculative draft holding `tokens`; None when its architecture is unknown.

    A draft's non-full layers are sliding-window attention, which also hold KV, so unlike
    the target's linear-attention layers they are counted: each `full_attention` layer grows
    with every token, each `sliding_attention` layer up to the declared window. Without
    `layer_types` every layer is capped at a declared window.
    """
    if not draft_config:
        return None
    cfg = text_config(draft_config)
    window = cfg.get("sliding_window")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        window = None
    windowed_tokens = min(tokens, window) if window else tokens
    layer_types = cfg.get("layer_types")
    if layer_types is None:
        per_token = theoretical_kv_bytes_per_token(cfg, kv_cache_dtype_bytes,
                                                   quantization_param_bytes)
        return None if per_token is None else per_token * windowed_tokens
    num_layers = cfg.get("num_hidden_layers")
    if (not isinstance(layer_types, (list, tuple)) or len(layer_types) != num_layers
            or any(t not in ("full_attention", "sliding_attention") for t in layer_types)):
        return None
    all_layers = theoretical_kv_bytes_per_token({**cfg, "layer_types": None},
                                                kv_cache_dtype_bytes, quantization_param_bytes)
    if all_layers is None:
        return None
    per_layer = all_layers / num_layers
    full = sum(1 for t in layer_types if t == "full_attention")
    return per_layer * (full * tokens + (num_layers - full) * windowed_tokens)


def estimate_cache(model_config: dict | None, fixed_state_bytes: int | None, ov_config,
                   tokens: int | None, state_rows: int = 1,
                   draft_config: dict | None = None, speculative: bool = False) -> dict:
    """Estimated persistent cache demand; ``{"gib": float|None, "reason": str|None, ...}``."""
    detail = {"gib": None, "reason": None, "tokens": tokens, "state_rows": state_rows,
              "kv_bytes_per_token": None, "fixed_state_bytes": fixed_state_bytes,
              "speculative_bytes": None}
    if not model_config:
        detail["reason"] = "config.json is missing or unreadable"
        return detail
    if not known_kv_precision(ov_config):
        detail["reason"] = f"KV_CACHE_PRECISION {ov_config.get('KV_CACHE_PRECISION')!r} has no known size"
        return detail
    dtype, quant = kv_cache_dtype_bytes(ov_config), kv_quantization_param_bytes(ov_config)
    per_token = theoretical_kv_bytes_per_token(model_config, dtype, quant)
    if per_token is None:
        detail["reason"] = "architecture (layers/heads/head_dim/layer_types) is unknown or inconsistent"
        return detail
    detail["kv_bytes_per_token"] = per_token
    if tokens is None:
        detail["reason"] = "the standalone prompt length is unknown before the child tokenizes it"
        return detail
    if is_hybrid(model_config) and not fixed_state_bytes:
        detail["reason"] = "hybrid model but the IR's linear-attention state could not be read"
        return detail
    total = per_token * tokens + (fixed_state_bytes or 0) * max(1, state_rows)

    draft_bytes = 0.0
    if speculative:
        draft_bytes = draft_kv_bytes(draft_config, tokens, dtype, quant)
        if draft_bytes is None:
            detail["reason"] = "the speculative draft architecture is unknown"
            return detail
    detail["speculative_bytes"] = draft_bytes
    detail["gib"] = (total + draft_bytes) / _GIB
    return detail


def state_rows(scheduler: dict | None) -> int:
    """Linear-attention state rows the pool reserves: the explicit row count when set,
    else one per schedulable sequence; 1 on the stateful pipeline."""
    if not scheduler:
        return 1
    rows = scheduler.get("num_linear_attention_blocks")
    if isinstance(rows, int) and rows > 0:
        return rows
    return scheduler.get("max_num_seqs", GENAI_DEFAULT_MAX_NUM_SEQS)


def auto_cache_size_gb(estimated_gib: float | None, weight_gib: float,
                       budget_gb: float | None) -> dict:
    """Size a `cache_size: auto` pool, or say why it cannot be sized.

    ``needed = max(2, 1.3 * estimate)``, ``pool = ceil(needed)``,
    ``ceiling = floor(budget - weights - 8)``. A pool above the ceiling is infeasible; it is
    never clamped below demand. Returns ``{"pool", "ceiling", "status", "reason"}``.
    """
    if estimated_gib is None:
        return {"pool": None, "ceiling": None, "status": "unknown",
                "reason": "the cache estimate is unavailable"}
    if budget_gb is None:
        return {"pool": None, "ceiling": None, "status": "unknown",
                "reason": "no GPU memory budget to size against"}
    needed = max(AUTO_CACHE_MIN_GB, AUTO_CACHE_SAFETY * estimated_gib)
    pool = int(math.ceil(needed))
    ceiling = int(math.floor(budget_gb - weight_gib - AUTO_CACHE_RESERVE_GB))
    if pool > ceiling:
        return {"pool": pool, "ceiling": ceiling, "status": "infeasible",
                "reason": (f"auto pool {pool} GiB exceeds the {ceiling} GiB left by the "
                           f"{budget_gb:g} GiB budget after {weight_gib:.1f} GiB of weights and "
                           f"{AUTO_CACHE_RESERVE_GB:g} GiB reserve")}
    return {"pool": pool, "ceiling": ceiling, "status": "ok", "reason": None}


def validate_fixed_cache_size(cache_size, kv_gb: float | None) -> None:
    """Reject a fixed scheduler pool smaller than the estimated persistent cache."""
    if cache_size is None or cache_size == "auto" or kv_gb is None:
        return
    if isinstance(cache_size, bool) or not isinstance(cache_size, (int, float)):
        raise ValueError(f"cache_size must be a whole number of GiB or 'auto', got {cache_size!r}")
    if cache_size < kv_gb:
        raise ValueError(
            f"cache_size={cache_size:g} GB is below the estimated {kv_gb:.2f} GB persistent "
            "cache; increase cache_size or, on a hybrid model where the estimate includes one "
            "linear-attention reservation per row, lower max_num_seqs"
        )


# ---------------------------------------------------------------------------
# Layout checks
# ---------------------------------------------------------------------------
def check_ir_layout(model_dir) -> list:
    """Missing files for the directory's IR layout; empty when complete.

    VLM layout (`openvino_language_model.xml`, for VLMPipeline) needs the language model,
    text embeddings and vision embeddings; plain layout needs `openvino_model.xml`. Both
    need tokenizer/detokenizer IRs, config.json and the HF tokenizer config, and every
    ``openvino_*.xml`` graph needs its ``.bin`` weights.
    """
    model_dir = str(model_dir)
    if not os.path.isdir(model_dir):
        return ["<directory>"]
    present = set(os.listdir(model_dir))
    if "openvino_language_model.xml" in present:
        required = ["openvino_language_model.xml", "openvino_text_embeddings_model.xml",
                    "openvino_vision_embeddings_model.xml"]
    elif "openvino_model.xml" in present:
        required = ["openvino_model.xml"]
    else:
        required = ["openvino_model.xml or openvino_language_model.xml"]
    required += ["openvino_tokenizer.xml", "openvino_detokenizer.xml", "config.json",
                 "tokenizer_config.json"]
    missing = [name for name in required if name not in present]
    for name in sorted(present):
        if name.startswith("openvino_") and name.endswith(".xml"):
            weights = name[:-4] + ".bin"
            if weights not in present:
                missing.append(weights)
    return missing


def check_draft_layout(draft_dir) -> list:
    draft_dir = str(draft_dir)
    if not os.path.isdir(draft_dir):
        return ["<directory>"]
    return [name for name in ("openvino_model.xml", "openvino_model.bin", "config.json")
            if not os.path.isfile(os.path.join(draft_dir, name))]

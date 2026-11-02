# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Serving catalog and ``models.text_gen.speculative`` resolution.

Pure data and validation -- no OpenVINO import -- so the launcher, the warm VLM
and the tests share one decision. :func:`resolve_plan` turns the ``text_gen``
config node into a :class:`TextGenPlan`:

* the **target** stays selected by ``vlm_name`` + ``weight_format``; a model the
  catalog does not know keeps the existing plain loading path untouched;
* the optional **speculative** block selects an activation policy (``mode``),
  a mechanism (``method``) and draft artifacts. Invalid settings raise
  :class:`SpeculativeConfigError`; they never trigger a fallback. A recognized
  method without a qualified profile becomes plain generation under
  ``mode: auto`` and an actionable failure under ``mode: "on"``.

Speculative profiles: DFlash for Qwen3.6-35B-A3B INT4, Eagle3 for
Qwen3-VL-8B-Instruct INT4, and for Qwen3.5-9B INT4/INT8 DFlash (the ``auto``
choice) or target-bundled MTP.
``dflash2`` stays a valid setting without a profile: plain generation under
``auto``, an actionable failure under ``"on"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional

from utils.model_paths import OPENVINO_MODELS_DIR, SC_ROOT, openvino_model_dir

MODES = ("off", "auto", "on")
METHODS = ("auto", "dflash", "dflash2", "eagle3", "mtp")
DRAFT_SOURCES = ("huggingface", "local", "target")
SPECULATIVE_KEYS = frozenset({
    "mode", "method", "draft_source", "draft_model", "draft_revision", "num_assistant_tokens",
})

# Target rt_info GenAI reads to locate the hidden states a DFlash drafter consumes.
DFLASH_TARGET_RT_INFO_KEY = "hidden_states_decoder_layers"

# Revisions become path components and pins; only full commit hashes qualify.
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class SpeculativeConfigError(ValueError):
    """``models.text_gen.speculative`` is malformed or contradicts the catalog."""


class UnsupportedRequestError(ValueError):
    """A request the active pipeline cannot honor; rejected before generation.

    Raised instead of silently changing the request (for example sampling on a
    greedy-only speculative pipeline, or tools on a model without tool support).
    """


@dataclass(frozen=True)
class SpeculativeProfile:
    """One target/method combination the serving runtime knows how to load."""

    method: str
    precisions: tuple = ("int4", "int8")
    default_source: str = "huggingface"
    draft_repo: Optional[str] = None
    # Immutable Hub commit. ``None`` until a maintainer pins one: preparation
    # then resolves the current commit once, records it in the manifest and
    # reuses that pin on every later start.
    draft_revision: Optional[str] = None
    # Recipe turning the downloaded drafter checkpoint into runtime IR.
    draft_export_weight_format: Optional[str] = None
    num_assistant_tokens: int = 3
    # weight_format -> default window where it differs from num_assistant_tokens.
    num_assistant_tokens_by_precision: Mapping = field(default_factory=dict)
    num_assistant_tokens_range: tuple = (1, 15)
    min_genai_version: str = "2026.3"
    ov_properties: Mapping = field(default_factory=dict)
    scheduler: Mapping = field(default_factory=dict)
    # ``app.language`` values the profile is qualified for; None = any language.
    # Under any other language the plan is plain (auto) or a startup error (on).
    languages: Optional[tuple] = None


@dataclass(frozen=True)
class TargetProfile:
    name: str
    # weight_format -> pre-converted OpenVINO IR repository
    ir_repos: Mapping
    tool_calls: bool = False
    # Method ``auto`` resolves to this, or, for a precision or draft source its
    # profile does not cover, to the first method in ``methods`` that does.
    # Never chosen by trial loading.
    default_method: Optional[str] = None
    methods: Mapping = field(default_factory=dict)


_GPU_PAGED = {"GPU_ENABLE_LARGE_ALLOCATIONS": "YES", "KV_CACHE_PRECISION": "f16"}

# Values from the pinned context_bench configuration (config_qwen3.6_35b.yaml).
# cache_size is left runtime-managed: classroom prompts vary far more than the
# fixed 8K benchmark context.
_QWEN36_DFLASH = SpeculativeProfile(
    method="dflash",
    precisions=("int4",),
    draft_repo="z-lab/Qwen3.6-35B-A3B-DFlash",
    draft_revision="f181eece646affea2c38b2765f1aaa01a9734ccd",
    draft_export_weight_format="int4",
    # Measured on the Arc B390 iGPU (context_bench, 30 s idle gap): k=5 is best for the
    # classroom summary at 8K (1.34x vs 1.29x at k=3) and for chat (1.43x vs 1.38x), and
    # keeps most of k=7's code/math gain (2.2-2.4x vs 1.8x at k=3). k=7 suits code/math only.
    num_assistant_tokens=5,
    # block_size 16 includes the seed token, and the 17 linear-attention rows
    # cover max_num_seqs + k + 1 up to k=15.
    num_assistant_tokens_range=(1, 15),
    min_genai_version="2026.3",
    ov_properties=_GPU_PAGED,
    scheduler={
        "max_num_batched_tokens": 65536,
        "max_num_seqs": 1,
        "num_linear_attention_blocks": 17,
        "enable_prefix_caching": False,
    },
)

# Eagle3 drafter (one Llama layer reading three target hidden states, 32k draft
# vocabulary) exported to INT4: 259 MB, and faster than INT8/FP16 in the sweep.
# Measured on the 20 EvalScope a_okvqa requests, greedy: offline on Arc 140T
# 13.3 -> 21.7 tok/s at k=4 (k=3 1.48x, k=5 1.58x); Arc B390 23.7 -> 35.9 tok/s
# (k=3 36.0). Through the service on Arc 140T: 13.2 -> 18.0 tok/s (1.36x).
# The drafter predicts English only: Chinese output accepts ~1.2 tokens per step
# (the taobao-mnn drafter ~1.4), 28-55% slower than plain on Arc 140T, and the
# paged pipeline adds ~30% to the first token of a 10K-token Chinese classroom
# prompt (decode speed without drafting is about the same as plain). The profile is therefore qualified
# for app.language en only; zh deployments keep the plain pipeline.
# The profile pins GPU_QUEUE_PRIORITY=MEDIUM over models.gpu_queue_priority: a
# draft/verify step submits many small GPU jobs, and on a LOW queue the Arc 140T
# decoded 72 ms/token instead of 55 at the same acceptance, no faster than plain.
# The KV cache keeps the runtime's default precision: f16 made 6.5K-token
# classroom prompts decode 10-25% slower and was not used in the measurements.
_QWEN3VL8B_EAGLE3 = SpeculativeProfile(
    method="eagle3",
    precisions=("int4",),
    draft_repo="AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3",
    draft_revision="29ffa460d80e1d88a015902ae0333ff87ffe3184",
    draft_export_weight_format="int4",
    num_assistant_tokens=4,
    num_assistant_tokens_range=(1, 8),
    # The VLM (inputs_embeds) Eagle3 path was qualified on 2026.4.1.
    min_genai_version="2026.4",
    ov_properties={"GPU_ENABLE_LARGE_ALLOCATIONS": "YES", "GPU_QUEUE_PRIORITY": "MEDIUM"},
    scheduler={"max_num_seqs": 1, "enable_prefix_caching": False},
    languages=("en",),
)

# Qwen3.5's pre-converted target repositories ship openvino_mtp_model.xml/bin.
# GenAI 2026.4.1 loads the bundled head through the same VLMPipeline as the
# target, so image requests keep working. Arc 140T, one replayed English lesson
# per setting, MEDIUM queue for both: summary decode 13.44 tok/s at k=2 vs 13.45
# plain (k=3 12.1, k=5 11.8). k=2 was 1.06-1.15x faster on mind map and report,
# but one topic-segmentation call fell to 8% acceptance and the lesson's text
# generation took 11% longer overall. Not a qualification.
_QWEN35_MTP = SpeculativeProfile(
    method="mtp",
    precisions=("int4", "int8"),
    default_source="target",
    num_assistant_tokens=2,
    num_assistant_tokens_range=(1, 15),
    min_genai_version="2026.4.1",
    ov_properties={"GPU_ENABLE_LARGE_ALLOCATIONS": "YES", "GPU_QUEUE_PRIORITY": "MEDIUM"},
    scheduler={"max_num_seqs": 1, "enable_prefix_caching": False},
)

_QWEN35_DFLASH = SpeculativeProfile(
    method="dflash",
    precisions=("int4", "int8"),
    draft_repo="z-lab/Qwen3.5-9B-DFlash",
    draft_revision="5fc3b3d474760f18c516db87d84c37edbfd3ede6",
    draft_export_weight_format="int4",
    num_assistant_tokens=5,
    # A verification pass costs more on the INT8 target: k=7 was ~10% faster than
    # k=5 on every task there, while on INT4 the two tied on summaries.
    num_assistant_tokens_by_precision={"int8": 7},
    num_assistant_tokens_range=(1, 15),
    min_genai_version="2026.4.1",
    ov_properties={"GPU_ENABLE_LARGE_ALLOCATIONS": "YES", "GPU_QUEUE_PRIORITY": "MEDIUM"},
    scheduler={
        "max_num_batched_tokens": 65536,
        "max_num_seqs": 1,
        "num_linear_attention_blocks": 17,
        "enable_prefix_caching": False,
    },
)

CATALOG: Mapping[str, TargetProfile] = {
    "qwen/qwen3-vl-8b-instruct": TargetProfile(
        name="Qwen/Qwen3-VL-8B-Instruct",
        ir_repos={"int4": "OpenVINO/Qwen3-VL-8B-Instruct-int4-ov",
                  "int8": "OpenVINO/Qwen3-VL-8B-Instruct-int8-ov"},
        tool_calls=True,
        default_method="eagle3",
        methods={"eagle3": _QWEN3VL8B_EAGLE3},
    ),
    "qwen/qwen3.5-9b": TargetProfile(
        name="Qwen/Qwen3.5-9B",
        ir_repos={"int4": "OpenVINO/Qwen3.5-9B-int4-ov",
                  "int8": "OpenVINO/Qwen3.5-9B-int8-ov"},
        tool_calls=True,
        default_method="dflash",
        methods={"dflash": _QWEN35_DFLASH, "mtp": _QWEN35_MTP},
    ),
    "qwen/qwen3.6-35b-a3b": TargetProfile(
        name="Qwen/Qwen3.6-35B-A3B",
        ir_repos={"int4": "OpenVINO/Qwen3.6-35B-A3B-int4-ov"},
        tool_calls=True,
        default_method="dflash",
        methods={"dflash": _QWEN36_DFLASH},
    ),
    "qwen/qwen3.8-27b": TargetProfile(
        name="Qwen/Qwen3.8-27B",
        ir_repos={"int4": "OpenVINO/Qwen3.8-27B-int4-ov",
                  "int8": "OpenVINO/Qwen3.8-27B-int8-ov"},
        tool_calls=True,
    ),
}


def target_profile(model_name) -> Optional[TargetProfile]:
    return CATALOG.get(str(model_name).strip().lower())


def preconverted_repo(model_name, weight_format) -> Optional[str]:
    profile = target_profile(model_name)
    if profile is None:
        return None
    return profile.ir_repos.get(str(weight_format).lower())


@dataclass(frozen=True)
class SpeculativeSettings:
    """Resolved, validated speculative selection for one target."""

    method: str
    source: str
    draft_model: Optional[str]          # Hub repo id or local directory
    draft_revision: Optional[str]       # explicit/pinned commit; None = resolve once
    num_assistant_tokens: int
    profile: SpeculativeProfile

    def draft_dir(self) -> Optional[Path]:
        """Runtime IR directory of the drafter."""
        if self.source == "local":
            return resolve_local_dir(self.draft_model)
        if self.source == "huggingface" and self.draft_model:
            fmt = self.profile.draft_export_weight_format or "int4"
            return OPENVINO_MODELS_DIR / self.draft_model.split("/")[-1] / fmt
        return None

    def describe(self) -> str:
        """Identity safe for health/logs: no local paths."""
        if self.source == "local":
            return f"local IR ({Path(str(self.draft_model)).name})"
        if self.source == "target":
            return "target-bundled MTP"
        rev = f"@{self.draft_revision[:12]}" if self.draft_revision else ""
        return f"{self.draft_model}{rev}"


@dataclass(frozen=True)
class TextGenPlan:
    model_name: str
    weight_format: str
    device: str
    target_dir: Path
    target: Optional[TargetProfile]
    mode: str
    requested_method: Optional[str]
    speculative: Optional[SpeculativeSettings]
    # Why the plan is plain although speculation was requested.
    plain_reason: Optional[str] = None

    @property
    def wants_speculative(self) -> bool:
        return self.mode != "off" and self.speculative is not None

    @property
    def tool_calls_declared(self) -> Optional[bool]:
        return self.target.tool_calls if self.target is not None else None

    def without_speculative(self, reason: str) -> "TextGenPlan":
        return replace(self, speculative=None, plain_reason=reason)


def resolve_local_dir(value) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (SC_ROOT / path)


def _get(node, key, default=None):
    if isinstance(node, Mapping):
        return node.get(key, default)
    return getattr(node, key, default)


def _as_mapping(node) -> Optional[dict]:
    if node is None:
        return None
    if isinstance(node, Mapping):
        return dict(node)
    if hasattr(node, "__dict__"):
        return dict(vars(node))
    raise SpeculativeConfigError(
        "models.text_gen.speculative must be a mapping of speculative settings"
    )


def _mode(value) -> str:
    # YAML 1.1 reads bare off/on as booleans; accept them for the policy field.
    if value is None:
        return "off"
    if value is False:
        return "off"
    if value is True:
        return "on"
    text = str(value).strip().lower()
    if text not in MODES:
        raise SpeculativeConfigError(
            f"speculative.mode must be one of {', '.join(MODES)} (got {value!r})"
        )
    return text


def _choice(key, value, allowed, default):
    if value is None:
        return default
    text = str(value).strip().lower()
    if text not in allowed:
        raise SpeculativeConfigError(
            f"speculative.{key} must be one of {', '.join(allowed)} (got {value!r})"
        )
    return text


def _optional_str(key, value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SpeculativeConfigError(f"speculative.{key} must be a non-empty string or null")
    return value.strip()


def resolve_plan(text_gen, language: Optional[str] = None) -> TextGenPlan:
    """Resolve ``models.text_gen`` into a validated serving plan.

    ``language`` is ``app.language``; a profile qualified for other languages
    only is not used (None skips the check).
    """
    if text_gen is None:
        raise ValueError("models.text_gen is not configured")
    model_name = str(_get(text_gen, "vlm_name"))
    weight_format = str(_get(text_gen, "weight_format")).lower()
    device = str(_get(text_gen, "device", "GPU")).upper()
    target = target_profile(model_name)
    base = dict(
        model_name=model_name,
        weight_format=weight_format,
        device=device,
        target_dir=openvino_model_dir(model_name, weight_format),
        target=target,
    )

    raw = _as_mapping(_get(text_gen, "speculative"))
    if raw is None:
        return TextGenPlan(mode="off", requested_method=None, speculative=None, **base)

    mode = _mode(raw.get("mode"))
    if mode == "off":
        # Dormant fields are ignored entirely: only the target is prepared.
        return TextGenPlan(mode="off", requested_method=None, speculative=None, **base)

    unknown = set(raw) - SPECULATIVE_KEYS
    if unknown:
        raise SpeculativeConfigError(
            "unknown speculative key(s): " + ", ".join(sorted(map(str, unknown)))
            + "; supported: " + ", ".join(sorted(SPECULATIVE_KEYS))
        )
    method = _choice("method", raw.get("method"), METHODS, "auto")
    source = _choice("draft_source", raw.get("draft_source"), DRAFT_SOURCES, None)
    draft_model = _optional_str("draft_model", raw.get("draft_model"))
    revision = _optional_str("draft_revision", raw.get("draft_revision"))
    tokens = raw.get("num_assistant_tokens")
    if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int)
                               or tokens < 1):
        raise SpeculativeConfigError(
            f"speculative.num_assistant_tokens must be a positive integer or null (got {tokens!r})"
        )
    if revision is not None:
        # Preparation, pins and the manifest compare lowercase hashes.
        revision = revision.lower()
        if not COMMIT_RE.match(revision):
            raise SpeculativeConfigError(
                "speculative.draft_revision must be a full 40-character commit hash "
                "(branches and tags are mutable)"
            )
    _check_source_shape(method, source, draft_model, revision)

    resolved_method = method
    if method == "auto":
        resolved_method = _auto_method(target, weight_format, source)
        if resolved_method is None:
            return _unqualified(base, mode, method, (
                f"{model_name} has no catalog-declared speculative method; "
                "it continues with plain generation"
            ))

    profile = target.methods.get(resolved_method) if target else None
    if profile is None:
        return _unqualified(base, mode, method, (
            f"no qualified {resolved_method} profile for {model_name}"
        ))

    source = source or profile.default_source
    # Re-check with the defaults filled in (method auto -> concrete method).
    _check_source_shape(resolved_method, source, draft_model, revision)
    if source == "huggingface":
        if draft_model is None:
            draft_model = profile.draft_repo
        elif profile.draft_repo is None or draft_model.lower() != profile.draft_repo.lower():
            raise SpeculativeConfigError(
                f"speculative.draft_model {draft_model!r} is not the qualified {resolved_method} "
                f"drafter for {model_name} (expected {profile.draft_repo!r})"
            )
        revision = revision or profile.draft_revision

    k = tokens if tokens is not None else profile.num_assistant_tokens_by_precision.get(
        weight_format, profile.num_assistant_tokens)
    low, high = profile.num_assistant_tokens_range
    if not low <= k <= high:
        raise SpeculativeConfigError(
            f"speculative.num_assistant_tokens={k} is outside the {resolved_method} "
            f"profile's supported range {low}..{high}"
        )

    settings = SpeculativeSettings(
        method=resolved_method,
        source=source,
        draft_model=draft_model,
        draft_revision=revision,
        num_assistant_tokens=k,
        profile=profile,
    )
    if weight_format not in profile.precisions:
        return _unqualified(base, mode, method, (
            f"{resolved_method} is not qualified for {model_name} {weight_format} "
            f"(qualified: {', '.join(profile.precisions)})"
        ))
    lang = str(language).strip().lower() if language is not None else None
    if lang is not None and profile.languages is not None and lang not in profile.languages:
        return _unqualified(base, mode, method, (
            f"{resolved_method} is not qualified for app.language {lang} "
            f"(qualified: {', '.join(profile.languages)}); its drafter predicts "
            "English, and plain generation is faster for this language"
        ))
    return TextGenPlan(mode=mode, requested_method=method, speculative=settings, **base)


def _auto_method(target: Optional[TargetProfile], weight_format: str,
                 source: Optional[str]) -> Optional[str]:
    """The catalog's choice for ``method: auto`` at this precision and draft source.

    ``draft_source: target`` can only mean a target-bundled head, so it selects
    the method that uses one.
    """
    if target is None or target.default_method is None:
        return None
    preferred = target.default_method
    for name in (preferred, *target.methods):
        profile = target.methods[name]
        if weight_format in profile.precisions and (
                source != "target" or profile.default_source == "target"):
            return name
    return preferred  # reported as unqualified below


def _check_source_shape(method, source, draft_model, revision) -> None:
    """Reject contradictory method/source/model/revision combinations."""
    if source == "target" and method not in ("mtp", "auto"):
        raise SpeculativeConfigError(
            "speculative.draft_source: target is valid only with method: mtp"
        )
    if method == "mtp":
        if source not in (None, "target"):
            raise SpeculativeConfigError(
                "MTP uses the target's bundled prediction head; draft_source must be "
                "target (or null), not an external drafter"
            )
        if draft_model is not None:
            raise SpeculativeConfigError(
                "MTP uses the target's bundled prediction head; draft_model must be null"
            )
    if source in ("local", "target") and revision is not None:
        raise SpeculativeConfigError(
            f"speculative.draft_revision must be null for draft_source: {source}"
        )
    if source == "local" and draft_model is None:
        raise SpeculativeConfigError(
            "speculative.draft_source: local requires draft_model to name an existing "
            "OpenVINO IR directory"
        )
    if source == "target" and draft_model is not None:
        raise SpeculativeConfigError(
            "speculative.draft_source: target requires draft_model: null"
        )


def _unqualified(base: dict, mode: str, method: str, reason: str) -> TextGenPlan:
    if mode == "on":
        raise SpeculativeUnavailableError(
            f"{reason}. speculative.mode is \"on\", which requires acceleration; "
            "set mode: auto to allow plain fallback or mode: \"off\" for plain generation"
        )
    return TextGenPlan(mode=mode, requested_method=method, speculative=None,
                       plain_reason=reason, **base)


class SpeculativeUnavailableError(RuntimeError):
    """``mode: "on"`` was requested but the selected acceleration cannot run."""


def version_at_least(installed: Optional[str], required: str) -> bool:
    """Compare leading numeric release components (``2026.5.0.0.dev2026`` >= ``2026.5``)."""
    if not installed:
        return False

    def parts(text):
        out = []
        for piece in str(text).split("."):
            digits = re.match(r"\d+", piece)
            if not digits:
                break
            out.append(int(digits.group()))
        return out

    have, need = parts(installed), parts(required)
    width = max(len(have), len(need))
    return have + [0] * (width - len(have)) >= need + [0] * (width - len(need))

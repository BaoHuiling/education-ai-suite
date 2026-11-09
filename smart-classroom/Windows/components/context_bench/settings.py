# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Benchmark CLI, configuration schema and run planning (re-exported by `benchmark.py`).

Resolution order: safe-load the benchmark YAML into plain mappings, apply CLI overrides,
validate the resolved schema strictly, then freeze it before any child is created.
Precedence is **CLI override > benchmark YAML > documented tool default**. Unknown keys,
booleans used as integers, non-finite numbers, duplicate keys and malformed mappings are
configuration errors. The application's configuration singleton is never imported; only the
`classroom_summary` task reads application settings, by safe-loading ``config.yaml``.

Nothing here imports OpenVINO, transformers or numpy, downloads anything or writes files, so
``--list-profiles`` is pure planning.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
from pathlib import Path

import yaml

from components.context_bench import accuracy, tasks, wwb_adapter

TOOL_DIR = Path(__file__).resolve().parent
SC_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = TOOL_DIR / "config_qwen3.6_35b.yaml"
APP_CONFIG_PATH = SC_ROOT / "config.yaml"

THROUGHPUT_TASKS = ("summary_2s", "classroom_summary", "math", "code", "chat")
# Tasks that summarize the transcript, so cannot run without one (`context_tokens: 0`).
TRANSCRIPT_TASKS = ("summary_2s", "classroom_summary")

# What each case decodes, printed beside the case plan.
TASK_DESCRIPTIONS = {
    "summary_2s": "two-sentence summary of the transcript (~45 tokens, stops at EOS)",
    "classroom_summary": "the application's summarizer request (prompts/summarizer; language "
                         "and mode from config.yaml), a long Markdown summary",
    "code": "a Python module with tests (HumanEval-like)",
    "math": "six worked physics/arithmetic/algebra problems (GSM8K-like)",
    "chat": "eight open-ended assistant requests (MT-Bench-like)",
}
STANDALONE_NOTE = "after the transcript, or the task prompt alone at context 0"
# Below this decode ceiling classroom_summary is cut off before its answer ends.
CLASSROOM_SUMMARY_TOKENS = 1024

PIPELINE_STATEFUL = "stateful"
PIPELINE_PAGED = "paged"

POOL_NONE = "none"          # stateful pipeline: no scheduler pool exists
POOL_RUNTIME = "runtime"    # cache_size omitted: OpenVINO manages the pool
POOL_FIXED = "fixed"
POOL_AUTO = "auto"

MODE_THROUGHPUT = "throughput"
MODE_ACCURACY = "accuracy"

_TOP_KEYS = ("model", "benchmark", "profiles", "accuracy", "speculative_reference")
_MODEL_KEYS = ("provider", "device", "weight_format", "models_base_path", "model_dirs")
_BENCHMARK_KEYS = (
    "models", "context_tokens", "output_tokens", "warmup", "iterations", "throughput_task",
    "iteration_gap_sec", "timeout_sec", "max_system_memory_pct", "gpu_memory_budget_gb",
    "cache_dir", "output_dir", "record_text",
)
# Speculative strategies, each a profile section of `_SPECULATIVE_KEYS` named after it.
SPECULATIVE_STRATEGIES = ("dflash", "eagle3")
_PROFILE_KEYS = ("name", "ov", "scheduler", *SPECULATIVE_STRATEGIES)
_SPECULATIVE_KEYS = ("enabled", "model", "num_assistant_tokens")
_ACCURACY_KEYS = (
    "suites", "baseline_profile", "seed", "embedding_model",
    accuracy.SUITE_RETRIEVAL, accuracy.SUITE_GENERATION,
)
_SUITE_KEYS = ("tasks", "depths", "samples", "output_tokens", "options")
_REFERENCE_KEYS = ("source", "num_assistant_tokens", "tasks")
_REFERENCE_TASK_KEYS = ("dataset", "acceptance_length", "speedup", "throughput")

# Speculative/runtime-level settings that must never leak into plugin properties.
_FORBIDDEN_OV_KEYS = ("draft_model", "scheduler_config", "num_assistant_tokens",
                      "assistant_confidence_threshold", "generation_config")
_REQUIRED_PAGED_KEYS = ("max_num_batched_tokens", "max_num_seqs", "enable_prefix_caching")

_DEVICE_PATTERN = re.compile(r"(CPU|GPU)(\.\d+)?", re.IGNORECASE)

TOOL_DEFAULTS = {
    "throughput_task": "summary_2s",
    "iteration_gap_sec": 0.0,
    "max_system_memory_pct": 80.0,
    "cache_dir": None,
    "record_text": False,
}

SPECULATIVE_OFF = {
    "enabled": False,
    "strategy": None,
    "model": None,
    "num_assistant_tokens": None,
}

_SUITE_DEFAULTS = {
    accuracy.SUITE_RETRIEVAL: {
        "tasks": list(tasks.RETRIEVAL_TASKS), "depths": [0.0, 0.5, 1.0],
        "samples": 2, "output_tokens": 64,
    },
    accuracy.SUITE_GENERATION: {
        "tasks": list(tasks.GENERATION_TASKS), "depths": [0.5],
        "samples": 2, "output_tokens": 256,
    },
}


class ConfigError(ValueError):
    """Invalid arguments or configuration: the run ends before anything loads (exit 2)."""


# ---------------------------------------------------------------------------
# Frozen settings
# ---------------------------------------------------------------------------
class FrozenDict(dict):
    """A dict that refuses mutation, so resolved settings cannot drift during a run."""

    def _blocked(self, *_args, **_kwargs):
        raise TypeError("resolved benchmark settings are frozen")

    __setitem__ = __delitem__ = _blocked
    clear = pop = popitem = setdefault = update = _blocked

    def __ior__(self, _other):
        raise TypeError("resolved benchmark settings are frozen")

    def __reduce__(self):
        return (dict, (dict(self),))


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    """A plain, mutable, JSON-shaped deep copy."""
    if isinstance(value, dict):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate and non-string mapping keys."""


def _construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ConfigError(f"line {key_node.start_mark.line + 1}: mapping keys must be strings")
        if key in mapping:
            raise ConfigError(f"line {key_node.start_mark.line + 1}: duplicate key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _plain(value, where: str):
    """Deep copy into str/int/float/bool/None, lists and dicts; anything else is rejected.

    Anchored YAML nodes are shared objects; copying here keeps one profile's resolution
    from mutating another's.
    """
    if isinstance(value, dict):
        return {key: _plain(item, f"{where}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{where} must be a finite number, got {value!r}")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ConfigError(f"{where} has unsupported YAML type {type(value).__name__}")


def load_yaml(path) -> dict:
    """Safe-load a YAML file into plain mappings; raises ConfigError."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=_StrictLoader)  # noqa: S506 - strict SafeLoader
    except OSError as exc:
        raise ConfigError(f"cannot read {Path(path).name}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{Path(path).name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{Path(path).name} must contain a mapping at the top level")
    return _plain(data, "config")


# ---------------------------------------------------------------------------
# Scalar validators
# ---------------------------------------------------------------------------
def _mapping(value, where: str, allow_none: bool = False) -> dict:
    if value is None and allow_none:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    return value


def _check_keys(mapping: dict, allowed, where: str, required=()) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ConfigError(
            f"{where} has unknown key(s): {', '.join(unknown)}. Accepted: {', '.join(allowed)}"
        )
    missing = [key for key in required if key not in mapping]
    if missing:
        raise ConfigError(f"{where} is missing required key(s): {', '.join(missing)}")


def _integer(value, where: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where} must be >= {minimum}, got {value}")
    return value


def _number(value, where: str, minimum: float | None = None, maximum: float | None = None,
            exclusive_minimum: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{where} must be a finite number, got {value!r}")
    if minimum is not None and (value <= minimum if exclusive_minimum else value < minimum):
        bound = ">" if exclusive_minimum else ">="
        raise ConfigError(f"{where} must be {bound} {minimum:g}, got {value:g}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{where} must be <= {maximum:g}, got {value:g}")
    return float(value)


def _string(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value.strip()


def _boolean(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false, got {value!r}")
    return value


def _scalar(value, where: str):
    if isinstance(value, (dict, list)) or value is None:
        raise ConfigError(f"{where} must be a string, number or boolean")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{where} must be finite")
    return value


def _device(value, where: str) -> str:
    device = _string(value, where)
    if not _DEVICE_PATTERN.fullmatch(device):
        raise ConfigError(f"{where} must be a CPU or GPU selector (CPU, GPU, GPU.1), got {device!r}")
    return device.upper()


def is_gpu(device: str) -> bool:
    return str(device).upper().startswith("GPU")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m components.context_bench.benchmark",
        description="Offline OpenVINO LLM benchmark: measure TTFT, TPOT, throughput and memory "
        "across runtime profiles, optionally with retrieval accuracy and generation fidelity.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="benchmark YAML (default: config_qwen3.6_35b.yaml beside this module)")
    parser.add_argument("--models", nargs="+", help="override benchmark.models")
    parser.add_argument("--contexts", type=int, nargs="+", help="override benchmark.context_tokens")
    parser.add_argument("--profiles", nargs="+", help="run only these profiles, by name")
    parser.add_argument("--device", help="override model.device (CPU, GPU, GPU.1)")
    parser.add_argument("--weight-format", help="override model.weight_format (label and "
                        "derived path; an explicit model_dirs entry is still validated)")
    parser.add_argument("--output-tokens", type=int, help="override benchmark.output_tokens")
    parser.add_argument("--warmup", type=int, help="override benchmark.warmup")
    parser.add_argument("--iterations", type=int, help="override benchmark.iterations")
    parser.add_argument("--iteration-gap", type=float, metavar="SECONDS",
                        help="override benchmark.iteration_gap_sec (untimed idle before each "
                        "measured generation)")
    parser.add_argument("--throughput-task", nargs="+", choices=THROUGHPUT_TASKS,
                        help="override benchmark.throughput_task")
    parser.add_argument("--output-dir", help="override benchmark.output_dir")
    parser.add_argument("--pipeline-config", nargs="+", metavar="KEY=VALUE",
                        help="merge string values into every selected profile's `ov`; KEY= removes")
    parser.add_argument("--scheduler-config", nargs="+", metavar="KEY=VALUE",
                        help="merge into every selected profile's `scheduler` (booleans and "
                        "numbers converted); KEY= removes")
    parser.add_argument("--assistant-tokens", dest="mtp_tokens", type=int,
                        metavar="K", help="num_assistant_tokens for every profile that already "
                        "speculates (dflash/eagle3); plain baselines stay plain")
    parser.add_argument("--accuracy", action="store_true",
                        help="run the accuracy suites from the config's `accuracy` section")
    parser.add_argument("--accuracy-suites", nargs="+", metavar="S",
                        choices=(accuracy.SUITE_RETRIEVAL, accuracy.SUITE_GENERATION),
                        help="run only these accuracy suites")
    parser.add_argument("--accuracy-tasks", nargs="+", metavar="T",
                        help=f"run only these accuracy tasks ({', '.join(tasks.KNOWN_TASKS)})")
    parser.add_argument("--accuracy-depths", type=float, nargs="+", metavar="D",
                        help="override the depths of the depth-swept tasks (fractions in [0,1])")
    parser.add_argument("--accuracy-samples", type=int, metavar="N",
                        help="override accuracy.<suite>.samples")
    parser.add_argument("--list-profiles", action="store_true",
                        help="print the resolved plan and exit -- no inference, downloads or files")
    return parser.parse_args(argv)


def _parse_overrides(items) -> dict:
    """``["KV_CACHE_PRECISION=f16", "CACHE_DIR="]`` -> a merge map; an empty value removes."""
    parsed = {}
    for item in items or []:
        key, sep, value = str(item).partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"expected KEY=VALUE (or KEY= to remove a key), got: {item!r}")
        parsed[key.strip()] = value.strip()
    return parsed


def _coerce(value: str):
    lowered = str(value).lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except ValueError:
        pass
    return value


def _apply_overrides(base: dict, overrides: dict, coerce: bool) -> dict:
    merged = dict(base)
    for key, value in (overrides or {}).items():
        if value == "":
            merged.pop(key, None)
        else:
            merged[key] = _coerce(value) if coerce else value
    return merged


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def pipeline_mode(scheduler_config) -> str:
    """Any SchedulerConfig selects paged attention; none selects the stateful pipeline."""
    return PIPELINE_PAGED if scheduler_config else PIPELINE_STATEFUL


def cache_pool(scheduler_config) -> str:
    if not scheduler_config:
        return POOL_NONE
    size = scheduler_config.get("cache_size")
    if size is None:
        return POOL_RUNTIME
    return POOL_AUTO if size == "auto" else POOL_FIXED


def _validate_ov(name: str, ov: dict) -> dict:
    for key, value in ov.items():
        if key in _FORBIDDEN_OV_KEYS:
            raise ConfigError(
                f"profile {name!r} ov.{key} is a speculative/runtime setting, not a plugin "
                "property; use the profile's dflash/eagle3/scheduler sections"
            )
        _scalar(value, f"profile {name!r} ov.{key}")
    return ov


def _validate_scheduler(name: str, scheduler: dict) -> dict:
    if not scheduler:
        return {}
    where = f"profile {name!r} scheduler"
    for key, value in scheduler.items():
        _scalar(value, f"{where}.{key}")
    missing = [key for key in _REQUIRED_PAGED_KEYS if key not in scheduler]
    if missing:
        raise ConfigError(
            f"{where} selects the paged pipeline but lacks {', '.join(missing)}; every paged "
            "profile needs positive integer max_num_batched_tokens and max_num_seqs and "
            "enable_prefix_caching: false"
        )
    _integer(scheduler["max_num_batched_tokens"], f"{where}.max_num_batched_tokens", 1)
    _integer(scheduler["max_num_seqs"], f"{where}.max_num_seqs", 1)
    if scheduler["enable_prefix_caching"] is not False:
        raise ConfigError(
            f"profile {name!r} enables prefix caching; repeated-prompt benchmark profiles "
            "must set enable_prefix_caching=false"
        )
    if "num_linear_attention_blocks" in scheduler:
        _integer(scheduler["num_linear_attention_blocks"],
                 f"{where}.num_linear_attention_blocks", 1)
    if "cache_size" in scheduler:
        size = scheduler["cache_size"]
        if isinstance(size, str) and size.strip().lower() == "auto":
            scheduler = {**scheduler, "cache_size": "auto"}
        else:
            _integer(size, f"{where}.cache_size (GiB, or 'auto')", 1)
    return scheduler


def _resolve_speculative(name: str, entry: dict, scheduler: dict, mtp_tokens) -> dict:
    present = [key for key in SPECULATIVE_STRATEGIES if entry.get(key) is not None]
    if not present:
        return dict(SPECULATIVE_OFF)
    if len(present) > 1:
        raise ConfigError(
            f"profile {name!r} has {' and '.join(present)} sections; a profile speculates "
            "with at most one strategy"
        )
    strategy = present[0]
    where = f"profile {name!r} {strategy}"
    section = _mapping(entry[strategy], where)
    _check_keys(section, _SPECULATIVE_KEYS, where)
    enabled = section.get("enabled", bool(section))
    _boolean(enabled, f"{where}.enabled")
    if not enabled:
        return dict(SPECULATIVE_OFF)

    tokens = mtp_tokens if mtp_tokens is not None else section.get("num_assistant_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 1:
        raise ConfigError(
            f"{where}.num_assistant_tokens must be an integer >= 1 (got "
            f"{tokens!r}); it is how many candidates the draft offers per verification pass"
        )
    if not scheduler:
        raise ConfigError(
            f"profile {name!r} enables {strategy} on the stateful pipeline; speculative "
            "decoding requires a paged `scheduler` section"
        )
    chunk = scheduler.get("max_num_batched_tokens")
    if chunk < tokens + 1:
        raise ConfigError(
            f"profile {name!r} has max_num_batched_tokens={chunk}, below the {tokens + 1} "
            f"tokens one speculative step submits (num_assistant_tokens={tokens} candidates "
            "plus the token being verified)"
        )
    model = _string(section.get("model"), f"{where}.model")
    linear_blocks = scheduler.get("num_linear_attention_blocks")
    required = scheduler["max_num_seqs"] + tokens + 1
    if linear_blocks is not None and linear_blocks < required:
        raise ConfigError(
            f"profile {name!r} has num_linear_attention_blocks={linear_blocks}, below the "
            f"{required} rows required by max_num_seqs={scheduler['max_num_seqs']} and "
            f"{strategy} num_assistant_tokens={tokens}"
        )
    return {"enabled": True, "strategy": strategy, "model": model,
            "num_assistant_tokens": tokens}


def _resolve_profiles(raw_profiles, names_filter=None, ov_overrides=None, sched_overrides=None,
                      mtp_tokens=None, notices: list | None = None) -> list:
    """Validate every profile's structure, select by name, then resolve the selection.

    Overrides apply to selected profiles and validation runs after them: removing a
    required property is an error. `--assistant-tokens` only changes profiles that already
    speculate. Returns plain dicts with ``{name, ov, scheduler, mtp, pipeline_mode,
    cache_pool}``; the speculative (dflash/eagle3) section keeps the reference's internal
    name `mtp`.
    """
    notices = notices if notices is not None else []
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ConfigError("`profiles` must be a non-empty list of profile mappings")
    if mtp_tokens is not None:
        _integer(mtp_tokens, "--assistant-tokens", 1)

    entries, names = [], []
    for index, entry in enumerate(raw_profiles):
        if not isinstance(entry, dict):
            raise ConfigError(f"profiles[{index}] must be a mapping")
        name = _string(entry.get("name"), f"profiles[{index}].name")
        unknown = sorted(set(entry) - set(_PROFILE_KEYS))
        if unknown:
            hint = (
                " `cache_size` belongs under `scheduler`, where it selects the paged pipeline; "
                "the stateful pipeline has no KV pool to cap."
                if "cache_size" in unknown else ""
            )
            raise ConfigError(
                f"profile {name!r} has unknown key(s): {', '.join(unknown)}. "
                f"A profile is {{{', '.join(_PROFILE_KEYS)}}}.{hint}"
            )
        for key in ("ov", "scheduler"):
            if entry.get(key) is not None and not isinstance(entry[key], dict):
                raise ConfigError(f"profile {name!r} ov and scheduler must be mappings")
        for strategy in SPECULATIVE_STRATEGIES:
            if entry.get(strategy) is not None and not isinstance(entry[strategy], dict):
                raise ConfigError(
                    f"profile {name!r} {strategy} must be a mapping of {_SPECULATIVE_KEYS}")
        entries.append(entry)
        names.append(name)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"profile names must be unique; duplicated: {', '.join(duplicates)}")

    if names_filter:
        wanted = [str(name).strip() for name in names_filter]
        unknown = sorted(set(wanted) - set(names))
        if unknown:
            raise ConfigError(
                f"unknown profile(s): {', '.join(unknown)}. Available: {', '.join(names)}"
            )
        selected = [(n, e) for n, e in zip(names, entries) if n in wanted]
    else:
        selected = list(zip(names, entries))

    resolved = []
    for name, entry in selected:
        original_scheduler = dict(entry.get("scheduler") or {})
        ov = _apply_overrides(dict(entry.get("ov") or {}), ov_overrides, coerce=False)
        scheduler = _apply_overrides(original_scheduler, sched_overrides, coerce=True)
        if pipeline_mode(original_scheduler) != pipeline_mode(scheduler):
            notices.append(
                f"--scheduler-config moved profile {name!r} from the "
                f"{pipeline_mode(original_scheduler)} pipeline to {pipeline_mode(scheduler)}"
            )
        ov = _validate_ov(name, ov)
        scheduler = _validate_scheduler(name, scheduler)
        speculative = _resolve_speculative(name, entry, scheduler, mtp_tokens)
        resolved.append({
            "name": name,
            "ov": ov,
            "scheduler": scheduler,
            "mtp": speculative,
            "pipeline_mode": pipeline_mode(scheduler),
            "cache_pool": cache_pool(scheduler),
        })
    _reject_identical_profiles(resolved)
    return resolved


def _profile_signature(profile: dict) -> str:
    import json

    return json.dumps({key: thaw(profile[key]) for key in ("ov", "scheduler", "mtp")},
                      sort_keys=True, default=str)


def _reject_identical_profiles(profiles: list) -> None:
    """Two selected profiles that resolve to one configuration would run every case twice.

    Typically `--assistant-tokens K` over a k-sweep: every speculative profile becomes k=K.
    """
    seen: dict = {}
    for profile in profiles:
        first = seen.setdefault(_profile_signature(profile), profile["name"])
        if first != profile["name"]:
            raise ConfigError(
                f"profiles {first!r} and {profile['name']!r} resolve to the same configuration "
                "(after CLI overrides), so every case would run twice; select one with "
                "--profiles"
            )


# ---------------------------------------------------------------------------
# Accuracy
# ---------------------------------------------------------------------------
def _resolve_accuracy(section, args, profiles: list, notices: list | None = None) -> dict | None:
    """Resolve the `accuracy` section, or None when --accuracy is off (it is not read then)."""
    notices = notices if notices is not None else []
    if not getattr(args, "accuracy", False):
        return None
    if not isinstance(section, dict) or not section:
        raise ConfigError(
            "--accuracy needs an `accuracy` section in the config; the minimum is "
            "`accuracy: {suites: [retrieval]}`"
        )
    _check_keys(section, _ACCURACY_KEYS, "accuracy")

    wanted = args.accuracy_suites if args.accuracy_suites else section.get(
        "suites", [accuracy.SUITE_RETRIEVAL, accuracy.SUITE_GENERATION]
    )
    if not isinstance(wanted, list) or not wanted or any(
        not isinstance(s, str) or s not in _SUITE_DEFAULTS for s in wanted
    ):
        raise ConfigError(
            f"accuracy.suites must be a non-empty subset of {{{', '.join(_SUITE_DEFAULTS)}}}"
        )
    if args.accuracy_tasks:
        for task in args.accuracy_tasks:
            if not tasks.is_known(task):
                raise ConfigError(
                    f"--accuracy-tasks has unknown task {task!r}; known: "
                    f"{', '.join(tasks.KNOWN_TASKS)}"
                )
    if args.accuracy_samples is not None:
        _integer(args.accuracy_samples, "--accuracy-samples", 1)

    seed = section.get("seed", 0)
    _integer(seed, "accuracy.seed")
    embedding = section.get("embedding_model", wwb_adapter.DEFAULT_EMBEDDING_MODEL)
    if embedding is not None:
        embedding = _string(embedding, "accuracy.embedding_model")

    resolved = {
        "suites": [s for s in _SUITE_DEFAULTS if s in wanted],
        "baseline_profile": None,
        "seed": seed,
        "embedding_model": embedding,
        accuracy.SUITE_RETRIEVAL: None,
        accuracy.SUITE_GENERATION: None,
    }
    for suite in list(resolved["suites"]):
        resolved[suite] = _resolve_suite(suite, section.get(suite), args)
        if resolved[suite] is None:
            resolved["suites"].remove(suite)
            notices.append(f"--accuracy-tasks selects no {suite} task; the {suite} suite is not run")
    if not resolved["suites"]:
        raise ConfigError(
            f"--accuracy-tasks {' '.join(args.accuracy_tasks)} selects no task in any suite "
            "being run"
        )

    baseline = section.get("baseline_profile")
    names = [p["name"] for p in profiles]
    # As the reference: the baseline is optional for every suite, but one that is named must be
    # run. Without it there are no retrieval deltas and the generation suite reports only its
    # grounded Coverage column (fidelity compares against the baseline's answers).
    if baseline is not None:
        baseline = _string(baseline, "accuracy.baseline_profile")
        if baseline not in names:
            raise ConfigError(
                f"accuracy.baseline_profile {baseline!r} is not one of the profiles being run: "
                f"{', '.join(names)}"
            )
    elif accuracy.SUITE_GENERATION in resolved["suites"]:
        notices.append(
            "accuracy.baseline_profile is not set: no retrieval deltas, and the generation "
            "suite reports only Coverage"
        )
    resolved["baseline_profile"] = baseline
    return resolved


def _resolve_suite(suite: str, section, args) -> dict | None:
    defaults = _SUITE_DEFAULTS[suite]
    section = _mapping(section, f"accuracy.{suite}", allow_none=True)
    _check_keys(section, _SUITE_KEYS, f"accuracy.{suite}")

    names = args.accuracy_tasks if args.accuracy_tasks else section.get("tasks", defaults["tasks"])
    if not isinstance(names, list) or not names:
        raise ConfigError(f"accuracy.{suite}.tasks must be a non-empty list of task names")
    for task in names:
        if not tasks.is_known(task):
            raise ConfigError(
                f"accuracy.{suite}.tasks has unknown task {task!r}. Known tasks are "
                f"retrieval: {', '.join(tasks.RETRIEVAL_TASKS)}; "
                f"generation: {', '.join(tasks.GENERATION_TASKS)}."
            )
    selected = list(dict.fromkeys(t for t in names if tasks.suite_of(t) == suite))
    if not selected:
        if args.accuracy_tasks:
            return None
        raise ConfigError(
            f"accuracy.{suite}.tasks selects no {suite} task; it lists only {', '.join(names)}"
        )

    depths = (list(args.accuracy_depths) if args.accuracy_depths is not None
              else section.get("depths", defaults["depths"]))
    if not isinstance(depths, list) or not depths:
        raise ConfigError(f"accuracy.{suite}.depths must be a non-empty list of fractions in [0.0, 1.0]")
    for depth in depths:
        _number(depth, f"accuracy.{suite}.depths (fractions in [0.0, 1.0])", 0.0, 1.0)

    samples = (args.accuracy_samples if args.accuracy_samples is not None
               else section.get("samples", defaults["samples"]))
    options = _mapping(section.get("options"), f"accuracy.{suite}.options", allow_none=True)
    resolved_options = {}
    for task, values in options.items():
        if not tasks.is_known(task) or tasks.suite_of(task) != suite:
            raise ConfigError(f"accuracy.{suite}.options names {task!r}, which is not a {suite} task")
        try:
            resolved_options[task] = tasks.validate_task_options(task, values)
        except ValueError as exc:
            raise ConfigError(f"accuracy.{suite}.{exc}") from exc
    return {
        "tasks": selected,
        "depths": sorted({float(d) for d in depths}),
        "samples": _integer(samples, f"accuracy.{suite}.samples", 1),
        "output_tokens": _integer(section.get("output_tokens", defaults["output_tokens"]),
                                  f"accuracy.{suite}.output_tokens", 1),
        "options": resolved_options,
    }


def _resolve_reference(section) -> dict | None:
    """Published speculative results printed beside the window sweep; display only."""
    if section is None:
        return None
    raw = _mapping(section, "speculative_reference")
    _check_keys(raw, _REFERENCE_KEYS, "speculative_reference", required=("num_assistant_tokens", "tasks"))
    k = raw.get("num_assistant_tokens")
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ConfigError("speculative_reference.num_assistant_tokens must be a positive integer")
    reference_tasks = raw.get("tasks")
    if not isinstance(reference_tasks, dict) or not reference_tasks:
        raise ConfigError("speculative_reference.tasks must map throughput tasks to results")
    resolved = {}
    for task, entry in reference_tasks.items():
        if task not in THROUGHPUT_TASKS:
            raise ConfigError(
                f"speculative_reference.tasks.{task}: not a throughput task ({', '.join(THROUGHPUT_TASKS)})"
            )
        entry = _mapping(entry, f"speculative_reference.tasks.{task}")
        _check_keys(entry, _REFERENCE_TASK_KEYS, f"speculative_reference.tasks.{task}")
        for field in ("acceptance_length", "speedup"):
            value = entry.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ConfigError(f"speculative_reference.tasks.{task}.{field} must be a positive number")
        throughput = entry.get("throughput")
        if throughput is not None:
            throughput = _number(throughput, f"speculative_reference.tasks.{task}.throughput",
                                 0, exclusive_minimum=True)
        resolved[task] = {
            "dataset": str(entry.get("dataset") or task),
            "acceptance_length": float(entry["acceptance_length"]),
            "speedup": float(entry["speedup"]),
            "throughput": throughput,
        }
    return {"source": str(raw.get("source") or "reference"), "num_assistant_tokens": k,
            "tasks": resolved}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _config_path(args) -> Path:
    path = Path(args.config)
    return path if path.is_absolute() else (Path.cwd() / path)


def _load_settings(args) -> FrozenDict:
    """Load, override, validate and freeze the run settings; raises ConfigError."""
    path = _config_path(args).resolve()
    raw = load_yaml(path)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return _resolve_settings(raw, args, str(path), digest)


def _resolve_settings(raw: dict, args, config_path: str | None = None,
                      config_sha256: str | None = None) -> FrozenDict:
    _check_keys(raw, _TOP_KEYS, "config", required=("model", "benchmark", "profiles"))
    notices: list = []

    model = _mapping(raw["model"], "model")
    _check_keys(model, _MODEL_KEYS, "model",
                required=("provider", "device", "weight_format", "models_base_path"))
    provider = _string(model["provider"], "model.provider")
    if provider != "openvino":
        raise ConfigError(f"model.provider must be 'openvino', got {provider!r}")
    device = _device(args.device if args.device else model["device"],
                     "--device" if args.device else "model.device")
    weight_format = _string(args.weight_format or model["weight_format"],
                            "--weight-format" if args.weight_format else "model.weight_format")
    models_base_path = _string(model["models_base_path"], "model.models_base_path")
    model_dirs = _mapping(model.get("model_dirs"), "model.model_dirs", allow_none=True)
    for name, directory in model_dirs.items():
        _string(directory, f"model.model_dirs.{name}")

    bench = _mapping(raw["benchmark"], "benchmark")
    _check_keys(bench, _BENCHMARK_KEYS, "benchmark",
                required=("models", "context_tokens", "output_tokens", "warmup", "iterations",
                          "timeout_sec", "output_dir"))

    def pick(cli_value, key, default=None, where=None):
        if cli_value is not None:
            return cli_value, where or f"--{key.replace('_', '-')}"
        if key in bench:
            return bench[key], f"benchmark.{key}"
        return default, f"benchmark.{key}"

    models, where = pick(args.models, "models")
    if not isinstance(models, list) or not models:
        raise ConfigError(f"{where} must contain at least one non-empty model name")
    models = [_string(name, where) for name in models]
    if len(set(models)) != len(models):
        raise ConfigError(f"{where} must not repeat a model name")

    contexts, where = pick(args.contexts, "context_tokens", where="--contexts")
    if not isinstance(contexts, list) or not contexts or any(
        isinstance(c, bool) or not isinstance(c, int) or c < 0 for c in contexts
    ):
        raise ConfigError(
            f"{where} must contain only positive integers, or 0 for no transcript"
        )
    contexts = sorted(set(contexts))

    output_tokens = _integer(*pick(args.output_tokens, "output_tokens"), minimum=2)
    warmup = _integer(*pick(args.warmup, "warmup"), minimum=0)
    iterations = _integer(*pick(args.iterations, "iterations"), minimum=1)
    gap = _number(*pick(args.iteration_gap, "iteration_gap_sec",
                        TOOL_DEFAULTS["iteration_gap_sec"], "--iteration-gap"), minimum=0.0)
    timeout = _number(bench["timeout_sec"], "benchmark.timeout_sec", 0, exclusive_minimum=True)
    if gap >= timeout:
        raise ConfigError(
            f"iteration_gap_sec={gap:g} must be smaller than timeout_sec={timeout:g}: the "
            "progress timeout also covers the idle gap"
        )

    configured_tasks, where = pick(args.throughput_task, "throughput_task",
                                   TOOL_DEFAULTS["throughput_task"])
    if isinstance(configured_tasks, str):
        configured_tasks = [configured_tasks]
    if not isinstance(configured_tasks, list) or not configured_tasks:
        raise ConfigError(f"{where} must name at least one task")
    for task in configured_tasks:
        if task not in THROUGHPUT_TASKS:
            raise ConfigError(f"{where} must be one of {', '.join(THROUGHPUT_TASKS)} (got {task!r})")
    throughput_tasks = list(dict.fromkeys(configured_tasks))

    max_ram_pct = _number(bench.get("max_system_memory_pct", TOOL_DEFAULTS["max_system_memory_pct"]),
                          "benchmark.max_system_memory_pct", 0, 100, exclusive_minimum=True)
    budget = bench.get("gpu_memory_budget_gb")
    if budget is not None:
        budget = _number(budget, "benchmark.gpu_memory_budget_gb", 0, exclusive_minimum=True)
    elif is_gpu(device):
        raise ConfigError("benchmark.gpu_memory_budget_gb (GiB) is required for GPU runs")
    cache_dir = bench.get("cache_dir", TOOL_DEFAULTS["cache_dir"])
    if cache_dir is not None:
        cache_dir = _string(cache_dir, "benchmark.cache_dir")
    output_dir = _string(args.output_dir or bench["output_dir"],
                         "--output-dir" if args.output_dir else "benchmark.output_dir")
    record_text = _boolean(bench.get("record_text", TOOL_DEFAULTS["record_text"]),
                           "benchmark.record_text")

    profiles = _resolve_profiles(
        raw["profiles"], args.profiles,
        _parse_overrides(args.pipeline_config), _parse_overrides(args.scheduler_config),
        args.mtp_tokens, notices,
    )
    accuracy_cfg = _resolve_accuracy(raw.get("accuracy"), args, profiles, notices)
    reference = _resolve_reference(raw.get("speculative_reference"))

    smoke = warmup == 0 or iterations == 1
    if smoke:
        notices.append("smoke test: zero warm-up or a single measured iteration is not a "
                       "repeated-measurement result")

    return freeze({
        "config_path": config_path,
        "config_sha256": config_sha256,
        "provider": provider,
        "device": device,
        "weight_format": weight_format,
        "models_base_path": models_base_path,
        "model_dirs": model_dirs,
        "models": models,
        "context_tokens": contexts,
        "output_tokens": output_tokens,
        "warmup": warmup,
        "iterations": iterations,
        "iteration_gap_sec": gap,
        "throughput_tasks": throughput_tasks,
        "throughput_task": throughput_tasks[0],
        "timeout_sec": timeout,
        "max_system_memory_pct": max_ram_pct,
        "gpu_memory_budget_gb": budget,
        "cache_dir": cache_dir,
        "output_dir": output_dir,
        "record_text": record_text,
        "profiles": profiles,
        "accuracy": accuracy_cfg,
        "speculative_reference": reference,
        "smoke_test": smoke,
        "notices": notices,
    })


# ---------------------------------------------------------------------------
# Throughput task framing
# ---------------------------------------------------------------------------
_TRANSCRIPT_PREFIX = "CLASSROOM TRANSCRIPT\n---\n"
_TRANSCRIPT_NOTE = " The lesson transcript is context only; "
# One representative prompt per published speculative-decoding domain (HumanEval-like code,
# GSM8K-like math, MT-Bench-like chat). Representative fixtures, not the datasets: compare
# within one task and report published dataset figures separately.
_DOMAIN_TASKS = {
    "math": {
        "role": "You are a mathematics teaching assistant.",
        "after_transcript": "solve the practice problems that follow it.",
        "lead": "The teacher handed out these practice problems after the lesson. ",
        "standalone_lead": "Here are six practice problems. ",
        "body": (
            "Solve each one step by step, showing every calculation, and end each solution "
            "with a line 'Answer: <number>'.\n"
            "1. A cart of mass 12 kg moves at 3 m/s. Its speed is doubled. By how many joules "
            "does its kinetic energy increase?\n"
            "2. A ball of mass 0.5 kg is dropped from a height of 20 m. Taking g = 9.8 m/s^2 "
            "and ignoring air resistance, what is its speed in m/s just before it hits the "
            "ground? Round to one decimal place.\n"
            "3. A school buys 14 boxes of markers with 24 markers in each box. 3 classes of "
            "28 students each receive 3 markers per student. How many markers are left?\n"
            "4. A train travels 180 km at 60 km/h and then 240 km at 80 km/h. What is its "
            "average speed for the whole journey in km/h?\n"
            "5. Solve for x: 3(2x - 5) + 4 = 5x + 7.\n"
            "6. The sum of three consecutive even integers is 138. What is the largest of the "
            "three?"
        ),
    },
    "code": {
        "role": "You are a programming teaching assistant.",
        "after_transcript": "write the code requested after it.",
        "lead": "",
        "standalone_lead": "",
        "body": (
            "Write a Python module for the class's physics lab with the following "
            "functions. Use type hints and a short docstring for each, and finish with "
            "unittest test cases covering every function.\n"
            "1. kinetic_energy(mass: float, velocity: float) -> float -- 0.5 * m * v**2; raise "
            "ValueError for a negative mass.\n"
            "2. potential_energy(mass: float, height: float, g: float = 9.81) -> float.\n"
            "3. impact_speed(height: float, g: float = 9.81) -> float -- speed of a body "
            "dropped from rest, ignoring air resistance.\n"
            "4. braking_distance(speed: float, deceleration: float) -> float -- raise "
            "ValueError when the deceleration is not positive.\n"
            "5. summarize_measurements(values: list[float]) -> dict[str, float] -- the count, "
            "mean, minimum, maximum and population standard deviation of a list of "
            "measurements; raise ValueError for an empty list."
        ),
    },
    # One request per MT-Bench category in its order; the word cap keeps all eight inside a
    # 512-token answer.
    "chat": {
        "role": "You are a helpful teaching assistant.",
        "after_transcript": "respond to the requests that follow it.",
        "lead": "",
        "standalone_lead": "",
        "body": (
            "Respond to each of the following eight requests in turn, under its number. Keep "
            "each answer under 60 words.\n"
            "1. Write a short, friendly note to parents announcing next month's school science "
            "fair and asking for volunteers.\n"
            "2. You are a guide at a natural history museum. Welcome a group of visitors to the "
            "dinosaur hall.\n"
            "3. Ana finished a race before Ben, and Cleo finished after Ben. Who finished last, "
            "and why?\n"
            "4. A recipe needs 3 eggs for every 2 cups of flour. How many eggs are needed for "
            "7 cups of flour?\n"
            "5. Write a Python function that returns the second largest distinct number in a "
            "list.\n"
            "6. Extract each person's name, age and city as a JSON list from: 'Maria, 34, lives "
            "in Lisbon; Tom, 28, lives in Leeds; Aiko, 41, lives in Osaka.'\n"
            "7. Explain why the sky is blue during the day but the sunset looks red.\n"
            "8. Summarize the main causes of the First World War in two or three sentences."
        ),
    },
}

def _classroom_summary_request(model_name: str, app_config_path=None) -> dict:
    """The application's current summarizer request applied to synthetic content.

    Matches `SummarizerComponent` without instantiating it or ModelManager: the system
    prompt is ``load_prompt("summarizer", app.language, mode)`` (mode normalized as the
    component does), the user turn is the transcript alone, the template renders with
    ``enable_thinking=False``, and the ``/no_think`` soft switch is added exactly when the
    application's own renderer (`utils.chat_prompt`) adds it -- for dense Qwen3 models.
    `app.language` and `models.summarizer.mode` are read by safe-loading ``config.yaml``.
    """
    path = Path(app_config_path) if app_config_path else APP_CONFIG_PATH
    app = load_yaml(path)
    try:
        language = app["app"]["language"]
        mode = app["models"]["summarizer"].get("mode", "dialog")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(
            "throughput_task 'classroom_summary' needs app.language and models.summarizer in "
            f"{path.name}"
        ) from exc
    language = _string(language, f"{path.name} app.language")
    mode = str(mode or "dialog").strip().lower()
    mode = mode if mode in ("teacher", "hybrid") else "dialog"
    try:
        from utils.model_family import is_qwen3_dense
        from utils.prompt_loader import load_prompt

        system_prompt = load_prompt("summarizer", language, mode)
    except (OSError, ImportError) as exc:
        raise ConfigError(
            f"throughput_task 'classroom_summary' could not resolve the summarizer template "
            f"for language {language!r}, mode {mode!r}: {type(exc).__name__}"
        ) from exc
    if not system_prompt.strip():
        raise ConfigError("the resolved summarizer template is empty")
    no_think = is_qwen3_dense(model_name)
    return {
        "name": "classroom_summary",
        "system_prompt": system_prompt,
        "user_prefix": "/no_think\n" if no_think else "",
        "suffix": "",
        "language": language,
        "summary_mode": mode,
        "template_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
    }


def throughput_task_prompt(name: str | None, model_name: str, app_config_path=None) -> dict | None:
    """The framing a throughput task puts around the transcript, for build_benchmark_prompt.

    `summary_2s` (None) is the built-in two-sentence summary; `math`, `code` and `chat` keep
    the transcript prefix and ask a domain task after it, with a `standalone` framing for
    ``context_tokens: 0``; `classroom_summary` is the application's summarizer request.
    """
    if name in (None, THROUGHPUT_TASKS[0]):
        return None
    if name in _DOMAIN_TASKS:
        spec = _DOMAIN_TASKS[name]
        return {
            "name": name,
            "system_prompt": spec["role"] + _TRANSCRIPT_NOTE + spec["after_transcript"],
            "user_prefix": _TRANSCRIPT_PREFIX,
            "suffix": "\n---\nTASK\n" + spec["lead"] + spec["body"],
            "standalone": {
                "system_prompt": spec["role"],
                "user": spec["standalone_lead"] + spec["body"],
            },
        }
    if name == "classroom_summary":
        return _classroom_summary_request(model_name, app_config_path)
    raise ConfigError(f"unknown throughput task {name!r}")


def task_variant(framing: dict | None) -> str | None:
    """The application settings a task's framing was resolved from (`classroom_summary`'s
    language and mode), or None for a fixed framing.

    Shown wherever the task is named: config.yaml can change between runs, and a Chinese and
    an English summary are different workloads (DFlash accepts far fewer tokens on the
    former) that would otherwise share one task name.
    """
    framing = framing or {}
    if not framing.get("language"):
        return None
    return f"{framing['language']}, {framing.get('summary_mode') or 'dialog'}"


def task_sha256(framing: dict | None) -> str:
    """Identity of a task's prompt framing, so different workloads never match."""
    import json

    payload = json.dumps(framing or {"name": "summary_2s"}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def resolve_path(path) -> Path:
    """A configured path, relative ones resolved from the Smart Classroom root."""
    candidate = Path(str(path))
    return candidate if candidate.is_absolute() else (SC_ROOT / candidate)


def model_ir_dir(settings, model_name: str) -> Path:
    """`model_dirs[name]` if given, else ``<models_base_path>/<provider>/<name>/<weight_format>``."""
    override = (settings.get("model_dirs") or {}).get(model_name)
    if override:
        return resolve_path(override)
    base = resolve_path(settings["models_base_path"])
    return base / settings["provider"] / model_name.split("/")[-1] / str(settings["weight_format"]).lower()


def run_tasks(settings, context: int | None = None) -> list:
    """Throughput tasks run at `context`; ``[None]`` for one accuracy case per profile.

    Context 0 is the no-transcript setting: accuracy suites and transcript summaries skip it.
    """
    if settings.get("accuracy"):
        return [] if context == 0 else [None]
    configured = list(settings.get("throughput_tasks") or [settings.get("throughput_task")
                                                            or THROUGHPUT_TASKS[0]])
    if context == 0:
        return [task for task in configured if task not in TRANSCRIPT_TASKS]
    return configured


def context_label(context: int) -> str:
    return f"{context:,} tokens" if context else "no transcript (task prompt only)"


def plan_run(settings, app_config_path=None) -> list:
    """Every case in execution order: model, context, task, then profile.

    Task outside profile, so a task's baseline and speculative profiles run back to back.
    An empty matrix is a configuration error.
    """
    planned = []
    accuracy_cfg = settings.get("accuracy")
    probe_total = accuracy.planned_probe_count(accuracy_cfg) if accuracy_cfg else 0
    for model in settings["models"]:
        model_dir = model_ir_dir(settings, model)
        framings = {}
        for context in settings["context_tokens"]:
            for task in run_tasks(settings, context):
                if task not in framings:
                    framings[task] = (throughput_task_prompt(task, model, app_config_path)
                                      if task else None)
                for profile in settings["profiles"]:
                    planned.append({
                        "case_id": None,
                        "label": f"{model} | {profile['name']} | {context_label(context)}"
                                 + (f" | {task}" if task else " | accuracy"),
                        "mode": MODE_ACCURACY if accuracy_cfg else MODE_THROUGHPUT,
                        "model": model,
                        "model_dir": str(model_dir),
                        "context_tokens": context,
                        "throughput_task": task,
                        "task_framing": framings[task],
                        "task_sha256": task_sha256(framings[task]) if task else None,
                        "task_variant": task_variant(framings[task]),
                        "profile": profile,
                        "warmup": settings["warmup"],
                        "planned_iterations": 0 if accuracy_cfg else settings["iterations"],
                        "planned_probes": probe_total if accuracy_cfg else 0,
                    })
    if not planned:
        raise ConfigError(
            "the filtered matrix is empty: context_tokens 0 (no transcript) runs only the "
            f"standalone tasks ({', '.join(t for t in THROUGHPUT_TASKS if t not in TRANSCRIPT_TASKS)}) "
            "and never the accuracy suites"
        )
    for number, case in enumerate(planned, 1):
        case["case_id"] = case_id(number, len(planned))
    return planned


def case_id(number: int, total: int) -> str:
    """`c01`, `c02`, ...: execution order, zero-padded to the plan size so IDs sort."""
    return f"c{number:0{max(2, len(str(total)))}d}"


def skipped_matrix_points(settings) -> list:
    """(context, task) pairs the configuration lists but context 0 cannot run."""
    if settings.get("accuracy"):
        return [(0, None)] if 0 in settings["context_tokens"] else []
    return [
        (0, task) for task in (settings.get("throughput_tasks") or ())
        if 0 in settings["context_tokens"] and task in TRANSCRIPT_TASKS
    ]


def format_config(config) -> str:
    if not config:
        return "(none)"
    return " ".join(f"{key}={value}" for key, value in sorted(dict(config).items()))


def format_speculative(mtp, device: str | None = None, redact=None) -> str:
    if not (mtp or {}).get("enabled"):
        return "off"
    where = f" on {device}" if device else ""
    model = mtp.get("model")
    if model:
        location = redact(resolve_path(model)) if redact else model
        model = f" model={location}"
    return f"{mtp['strategy']} num_assistant_tokens={mtp['num_assistant_tokens']}{model or ''}{where}"


def _profile_lines(profile: dict, settings, redact) -> dict:
    pool = {"auto": "auto (resolved at preflight)", "fixed": "fixed",
            "runtime": "runtime-managed", "none": "no scheduler pool"}[profile["cache_pool"]]
    return {
        "pipeline": f"{profile['pipeline_mode']} | cache pool: {pool}",
        "ov": format_config(profile["ov"]),
        "scheduler": format_config(profile["scheduler"]),
        "speculative": format_speculative(profile["mtp"], settings["device"], redact),
    }


def profile_summary(settings, redact) -> list:
    """Profile settings, each field printed once when every profile shares it."""
    profiles = settings["profiles"]
    described = {p["name"]: _profile_lines(p, settings, redact) for p in profiles}
    fields = ("pipeline", "ov", "scheduler", "speculative")
    shared = [f for f in fields
              if len(profiles) > 1 and len({lines[f] for lines in described.values()}) == 1]
    own = [f for f in fields if f not in shared] or ["speculative"]
    lines = [f"{len(profiles)} profile(s): {', '.join(described)}"]
    if shared:
        lines.append("  shared by every profile:")
        first = next(iter(described.values()))
        lines += [f"    {field + ':':<13}{first[field]}" for field in shared]
    width = max(len(name) for name in described)
    for name, entry in described.items():
        lines.append(f"  {name:<{width}}  {own[0] + ':':<13}{entry[own[0]]}")
        lines += [f"  {' ' * width}  {field + ':':<13}{entry[field]}" for field in own[1:]]
    return lines


def plan_grid(entries: list) -> list:
    """The case plan as a grid: one row per (model, context, task), one column per profile.

    `entries` are planned cases (or the plan exported to `results.json`) in execution order,
    which is row by row: every profile runs a task back to back before the next task starts.
    """
    def name_of(entry):
        profile = entry["profile"]
        return profile["name"] if isinstance(profile, dict) else profile

    profiles = list(dict.fromkeys(name_of(e) for e in entries))
    several_models = len({e["model"] for e in entries}) > 1
    rows: dict = {}
    for entry in entries:
        key = (entry["model"], entry["context_tokens"], entry.get("throughput_task") or "accuracy")
        rows.setdefault(key, {})[name_of(entry)] = entry["case_id"]
    head = (["Model"] if several_models else []) + ["Context", "Task"] + profiles
    body = [
        ([model] if several_models else [])
        + [f"{context:,} tokens" if context else "none (task only)", task]
        + [cells.get(name, "--") for name in profiles]
        for (model, context, task), cells in rows.items()
    ]
    widths = [max(len(str(row[i])) for row in [head] + body) for i in range(len(head))]
    fixed = len(head) - len(profiles)

    def render(row):
        cells = [f"{str(v):<{w}}" for v, w in zip(row, widths)]
        return f"  {'  '.join(cells[:fixed])} | {'  '.join(cells[fixed:])}".rstrip()

    return [render(head)] + [render(row) for row in body]


def task_legend(settings, planned: list) -> list:
    """One line per task that runs, saying what its cases decode."""
    lines = []
    variants = {p["throughput_task"]: p.get("task_variant") for p in planned}
    for task in variants:
        if task is None:
            acc = settings["accuracy"]
            suites = "; ".join(f"{suite}: {', '.join(acc[suite]['tasks'])}"
                               for suite in acc["suites"])
            lines.append(f"  {'accuracy':<18} {accuracy.planned_probe_count(acc)} probe(s) per "
                         f"case ({suites}); no timed iterations, latency and memory come from "
                         "the probes")
        else:
            where = "" if task in TRANSCRIPT_TASKS else f"; {STANDALONE_NOTE}"
            if task == "classroom_summary" and settings["output_tokens"] < CLASSROOM_SUMMARY_TOKENS:
                where = (f"; cut off at {settings['output_tokens']} output tokens, run it with "
                         f"--output-tokens {CLASSROOM_SUMMARY_TOKENS}")
            if variants[task]:
                where += f"; resolved as {variants[task]}"
            lines.append(f"  {task:<18} {TASK_DESCRIPTIONS[task]}{where}")
    return lines


def print_plan(settings, planned: list, redact) -> None:
    """The resolved plan, with redacted locations; artifact-dependent values stay unresolved."""
    print(f"Config: {redact(settings['config_path'])}")
    print(f"Device: {settings['device']} | Weights label: {settings['weight_format']}")
    budget = settings.get("gpu_memory_budget_gb")
    print(f"Budgets: host RAM <= {settings['max_system_memory_pct']:g}%"
          + (f", GPU <= {budget:g} GiB" if is_gpu(settings["device"]) else
             " (CPU run: no GPU budget enforced)"))
    print("Models (artifact checks run at preflight, not during planning):")
    for model in settings["models"]:
        print(f"  {model}: {redact(model_ir_dir(settings, model))}")
    gap = settings["iteration_gap_sec"]
    accuracy_cfg = settings.get("accuracy")
    per_case = ("the accuracy probes at their per-suite decode ceilings" if accuracy_cfg else
                f"{settings['iterations']} measured, up to {settings['output_tokens']} output "
                "tokens (EOS respected)")
    print(f"Each case: {settings['warmup']} warmup + {per_case}; progress timeout "
          f"{settings['timeout_sec']:g}s; "
          + (f"{gap:g}s idle before each measured run" if gap else "back-to-back (sustained)"))
    if settings.get("smoke_test"):
        print("  NOTE: smoke test -- not a repeated-measurement result")
    print(f"Record text: {'yes (synthetic answers persisted)' if settings['record_text'] else 'no'}")
    print()
    for line in profile_summary(settings, redact):
        print(line)

    kind = "accuracy" if accuracy_cfg else "throughput"
    print(f"\nCases: {len(planned)} {kind} case(s), run row by row (each task on every "
          "profile back to back):")
    for line in plan_grid(planned):
        print(line)
    for point in skipped_matrix_points(settings):
        print(f"  not planned at {context_label(point[0])}: {point[1] or 'accuracy suites'} "
              "(needs a transcript)")
    print("Tasks:")
    for line in task_legend(settings, planned):
        print(line)

    if accuracy_cfg:
        print(f"\nAccuracy suites: {', '.join(accuracy_cfg['suites'])} | baseline "
              f"{accuracy_cfg.get('baseline_profile') or '(none)'} | seed {accuracy_cfg['seed']}")
        for suite in accuracy_cfg["suites"]:
            section = accuracy_cfg[suite]
            count = sum(tasks.probe_count(t, section["samples"], section["depths"])
                        for t in section["tasks"])
            print(f"  {suite}: {count} probe(s) per case -- {', '.join(section['tasks'])}; "
                  f"{section['samples']} sample(s), depths {list(section['depths'])} "
                  f"(depth-swept tasks only), {section['output_tokens']} output tokens")
        print(f"  similarity: {accuracy_cfg.get('embedding_model') or 'off (embedding_model: null)'}")
    for notice in settings.get("notices") or ():
        print(f"[note] {notice}")

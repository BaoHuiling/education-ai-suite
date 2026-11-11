# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Offline, text-only OpenVINO LLM benchmark for Smart Classroom.

Answers, per (model, precision, device, context, task, profile):

  * can this machine process the context within the configured memory budgets, and
  * which runtime profile -- KV precision, stateful vs paged pipeline, scheduler, DFlash or
    Eagle3 speculative decoding and its window -- gives the best measured latency;

and, with ``--accuracy``, how a profile affects synthetic retrieval accuracy and generation
fidelity relative to a baseline profile. It never loads the classroom application, starts
services, modifies application configuration or selects production defaults.

Method (llm_bench): warm-up iterations excluded from every statistic, N measured iterations
over one reused prompt, median (p50) with mean, nearest-rank p95, min/max and raw samples.
Each case runs serially in a fresh spawned child (`trial_runner`); the parent samples memory
(`supervisor`), checkpoints versioned evidence (`results.json`) atomically before trials,
after every record and case and in ``finally``, and renders `report.txt` from the same
snapshot (`report`). Configuration, schema and planning live in `settings`; IR introspection
and cache sizing in `sizing`.

    python -m components.context_bench.benchmark --list-profiles
    .\\components\\context_bench\\run_benchmark.ps1 --config components/context_bench/config_qwen3.8_27b.yaml

Exit codes: 0 valid plan or completed run with every case ``ok``; 1 failed, partial or
unqualified cases, or an orchestration/output-write failure; 2 invalid arguments,
configuration or missing dependencies; 130 Ctrl-C after checkpoint and child cleanup.

See smart-classroom/docs/dev-guide/context-bench/context-bench-guide.md.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

from components.context_bench import accuracy, evidence, metrics, report, scoring, trial_runner
from components.context_bench.settings import (
    MODE_ACCURACY,
    MODE_THROUGHPUT,
    PIPELINE_PAGED,
    POOL_AUTO,
    POOL_FIXED,
    SC_ROOT,
    TOOL_DIR,
    ConfigError,
    _parse_args,
    format_config,
    is_gpu,
    plan_run,
    print_plan,
    resolve_path,
    skipped_matrix_points,
    thaw,
)
from components.context_bench.settings import _load_settings
from components.context_bench.sizing import (
    auto_cache_size_gb,
    check_draft_layout,
    check_ir_layout,
    estimate_cache,
    fixed_state_cache_bytes,
    ir_weight_precision,
    load_model_config,
    precision_matches_label,
    state_rows,
    validate_fixed_cache_size,
    weight_disk_gb,
)
from components.context_bench.supervisor import (
    CaseInterrupted,
    ChildCleanupError,
    _run_case_subprocess,
    _wait_for_memory_settle,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

_BACKEND_VENV_PYTHON = SC_ROOT.parent / "smartclassroom" / "Scripts" / "python.exe"
_LAUNCHER_SCRIPT = TOOL_DIR / "run_benchmark.ps1"
_SETUP_SCRIPT = SC_ROOT / "setup-smart-classroom.ps1"
_REQUIRED_MODULES = ("openvino", "openvino_genai", "transformers", "jinja2", "psutil")
_RUNTIME_PACKAGES = ("openvino", "openvino-genai", "openvino-tokenizers", "transformers",
                     "tokenizers", "psutil", "PyYAML", "numpy", "whowhatbench")

_EXPLICIT_FAILURES = ("oom", "gpu_abort", "unsupported", "timeout", "crashed", "missing_ir")
_DERIVED_STATUSES = ("no_output", "partial", "measurement_error", "gpu_memory_limit",
                     "memory_limit")
_ANSWER_ENCODERS: dict = {}


class DependencyError(RuntimeError):
    """The interpreter cannot import what the child needs (exit 2)."""


class _Rejected(Exception):
    """A case refused at preflight, before any child is spawned."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
def _preflight_environment_check() -> None:
    missing = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if not missing:
        return
    redactor = evidence.PathRedactor(SC_ROOT)
    lines = [
        f"Missing required package(s) in this interpreter: {', '.join(missing)}.",
        "The OpenVINO stack lives in the backend venv (<app root>/../smartclassroom), "
        "not the interpreter on PATH. Use the launcher, which runs that venv's Python:",
        f"  {redactor.redact(_LAUNCHER_SCRIPT)} <same arguments>",
        f"or prepare the venv first with {redactor.redact(_SETUP_SCRIPT)}",
        "(--list-profiles checks the run matrix without any of these packages.)",
    ]
    raise DependencyError("\n".join(lines))


def _tool_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(SC_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and len(commit) == 40 else None


def _environment() -> dict:
    memory_gb = None
    try:
        import psutil

        memory_gb = math.ceil(psutil.virtual_memory().total / (1024 ** 3))
    except Exception:  # noqa: BLE001
        pass
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine() or None,
        "processor": platform.processor() or None,
        "memory_gb": memory_gb,
        "packages": evidence.package_versions(_RUNTIME_PACKAGES),
        "device_names": [],
    }


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def _preparation_hint(model: str, model_dir: Path, missing: list, redact) -> str:
    return (
        f"{model}: IR at {redact(model_dir)} is incomplete (missing {', '.join(missing)}). "
        "Prepare it with the serving preparation workflow (start-smart-classroom.ps1, which "
        "places IR under models/openvino/<name>/<weight_format>) or an explicit export, then "
        "point model.model_dirs at it. The benchmark never downloads, exports or repairs files."
    )


def _model_static(settings, model: str, model_dir: Path, redactor) -> dict:
    """Local, read-only facts about one target artifact, gathered before its first load."""
    missing = check_ir_layout(model_dir)
    static = {"model_dir": model_dir, "missing": missing}
    if missing:
        static["hint"] = _preparation_hint(model, model_dir, missing, redactor.redact)
        static["identity"] = {"location": redactor.redact(model_dir), "artifact_id": None}
        return static
    precision = ir_weight_precision(model_dir)
    config = load_model_config(model_dir)
    static.update({
        "identity": evidence.artifact_identity(str(model_dir), redactor),
        "weight_disk_gb": weight_disk_gb(model_dir),
        "model_config": config,
        "fixed_state_bytes": fixed_state_cache_bytes(model_dir),
        "weight_precision": precision,
        "precision_label_mismatch": not precision_matches_label(precision, settings["weight_format"]),
        "layout": "vlm" if (model_dir / "openvino_language_model.xml").is_file() else "llm",
    })
    return static


def _token_budget(planned: dict, settings, mtp: dict) -> int | None:
    context = planned["context_tokens"]
    if not context:
        return None
    if planned["mode"] == MODE_ACCURACY:
        cfg = settings["accuracy"]
        ceiling = max(cfg[s]["output_tokens"] for s in cfg["suites"])
        tokens = context + trial_runner.PROBE_TOKEN_TOLERANCE + ceiling
    else:
        tokens = context + settings["output_tokens"]
    if mtp.get("enabled"):
        tokens += mtp["num_assistant_tokens"] + 1
    return tokens


# Where the serving preparation writes each strategy's drafter IR.
_DRAFT_LOCATIONS = {
    "dflash": "models/openvino/<name>-DFlash/<weight_format>, its own-head copy beside it "
              "in <weight_format>-lmhead",
    "eagle3": "models/openvino/<drafter repo name>/int4, e.g. Qwen3-VL-8B-Instruct-eagle3",
}


def _preflight_dflash(model_dir: Path, draft_dir: Path, tokens: int) -> None:
    """The serving preparation's DFlash pair check plus the window and target metadata."""
    # Layer ids, target depth and vocabulary. k=1 never trips its block-size check, which is
    # applied below as `unsupported`.
    from components.vlm.text_gen_prepare import PreparationError, validate_dflash_pair

    try:
        validate_dflash_pair(model_dir, draft_dir, 1)
    except PreparationError as exc:  # an incompatible pair or an unreadable config.json
        raise _Rejected("artifact", str(exc)) from exc
    block_size = trial_runner.dflash_block_size(draft_dir)
    if block_size and tokens > block_size - 1:
        raise _Rejected("unsupported", (
            f"num_assistant_tokens={tokens} exceeds the draft's block_size {block_size} minus "
            f"the seed token (max {block_size - 1})"
        ))
    target_xml = trial_runner.language_model_ir(str(model_dir))
    has_metadata = trial_runner.has_model_rt_info(target_xml, trial_runner.DFLASH_TARGET_RT_INFO_KEY)
    if has_metadata is False:  # None: unreadable XML, left to the runtime to judge
        hint = trial_runner.DFLASH_METADATA_HINT
        derived = model_dir.with_name(model_dir.name + "-dflash")
        if derived.is_dir():
            hint += (f" A derived target already exists at "
                     f"{evidence.PathRedactor(SC_ROOT).redact(derived)}.")
        raise _Rejected("missing_ir", hint)


def _preflight_eagle3(model_dir: Path, draft_dir: Path) -> None:
    """The serving preparation's Eagle3 pair check: architecture, target model type, hidden
    size and vocabulary, and the VLM drafter IR interface. The runtime taps the target's
    hidden states itself, so the ordinary target is used as-is."""
    from components.vlm.text_gen_prepare import PreparationError, validate_eagle3_pair

    try:
        validate_eagle3_pair(model_dir, draft_dir)
    except (PreparationError, ET.ParseError, OSError) as exc:
        raise _Rejected("artifact", str(exc)) from exc


def _preflight_case(planned: dict, settings, static: dict) -> dict:
    """Artifact, speculative and cache checks for one case; raises `_Rejected`."""
    profile = planned["profile"]
    mtp = dict(profile["mtp"])
    if static.get("missing"):
        raise _Rejected("missing_ir", static["hint"])
    model_dir = static["model_dir"]
    prepared = {"warnings": [], "draft_dir": None, "draft_weight_precision": None,
                "draft_weight_gb": 0.0}
    draft_config = None
    if mtp.get("enabled"):
        draft_dir = resolve_path(mtp["model"])
        prepared["draft_dir"] = draft_dir
        missing = check_draft_layout(draft_dir)
        if missing:
            raise _Rejected("missing_ir", (
                f"{mtp['strategy']} draft IR is incomplete (missing {', '.join(missing)}); the "
                f"serving workflow exports it ({_DRAFT_LOCATIONS[mtp['strategy']]})"
            ))
        if mtp["strategy"] == "eagle3":
            _preflight_eagle3(model_dir, draft_dir)
        else:
            _preflight_dflash(model_dir, draft_dir, mtp["num_assistant_tokens"])
        draft_config = load_model_config(draft_dir)
        prepared["draft_weight_precision"] = ir_weight_precision(draft_dir, ("openvino_model.xml",))
        prepared["draft_weight_gb"] = weight_disk_gb(draft_dir)

    ov = dict(profile["ov"])
    if settings.get("cache_dir") and "CACHE_DIR" not in ov:
        ov["CACHE_DIR"] = str(resolve_path(settings["cache_dir"]))
    scheduler = dict(profile["scheduler"])
    pool = profile["cache_pool"]
    estimate = estimate_cache(
        static["model_config"], static["fixed_state_bytes"], ov,
        _token_budget(planned, settings, mtp), state_rows=state_rows(scheduler),
        draft_config=draft_config, speculative=bool(mtp.get("enabled")),
    )
    prepared.update({"estimate": estimate, "ceiling_gb": None})
    if pool == POOL_FIXED:
        if estimate["gib"] is None:
            prepared["warnings"].append(
                f"fixed cache_size={scheduler['cache_size']} GiB was not validated: estimate "
                f"unavailable ({estimate['reason']})"
            )
        else:
            try:
                validate_fixed_cache_size(scheduler["cache_size"], estimate["gib"])
            except ValueError as exc:
                raise _Rejected("cache_too_small", str(exc)) from exc
    elif pool == POOL_AUTO:
        if not is_gpu(settings["device"]):
            raise _Rejected("cache_unknown", (
                "cache_size: auto is not supported on CPU; set an explicit cache_size or omit "
                "it for a runtime-managed pool"
            ))
        if mtp.get("enabled"):
            raise _Rejected("cache_unknown", (
                f"cache_size: auto has no qualified pool factor for {mtp['strategy']}; set "
                "an explicit cache_size or omit it for a runtime-managed pool"
            ))
        sized = auto_cache_size_gb(estimate["gib"], static["weight_disk_gb"] + prepared["draft_weight_gb"],
                                   settings.get("gpu_memory_budget_gb"))
        prepared["ceiling_gb"] = sized["ceiling"]
        if sized["status"] == "infeasible":
            raise _Rejected("cache_infeasible", sized["reason"])
        if sized["status"] != "ok":
            raise _Rejected("cache_unknown", (
                f"cache_size: auto cannot be sized ({sized['reason']}: "
                f"{estimate['reason'] or 'unknown'}); set an explicit cache_size or omit it"
            ))
        scheduler["cache_size"] = sized["pool"]
    prepared.update({"ov": ov, "scheduler": scheduler, "mtp": mtp})
    return prepared


# ---------------------------------------------------------------------------
# Case assembly
# ---------------------------------------------------------------------------
def _new_case(planned: dict, settings, static: dict, redactor) -> dict:
    profile = planned["profile"]
    mtp = profile["mtp"]
    identity = static.get("identity") or {}
    return {
        "case_id": planned["case_id"],
        "label": planned["label"],
        "mode": planned["mode"],
        "model": planned["model"],
        "artifact_id": identity.get("artifact_id"),
        "model_location": identity.get("location"),
        "device": settings["device"],
        "weight_format": settings["weight_format"],
        "weight_precision": static.get("weight_precision"),
        "precision_label_mismatch": bool(static.get("precision_label_mismatch")),
        "context_tokens": planned["context_tokens"],
        "throughput_task": planned["throughput_task"],
        "task_sha256": planned["task_sha256"],
        "task_variant": planned.get("task_variant"),
        "profile": profile["name"],
        "pipeline_mode": profile["pipeline_mode"],
        "cache_pool": profile["cache_pool"],
        "kv_cache_precision": profile["ov"].get("KV_CACHE_PRECISION", "(plugin default)"),
        "mtp": bool(mtp["enabled"]),
        "speculative_strategy": mtp["strategy"],
        "num_assistant_tokens": mtp["num_assistant_tokens"],
        "mtp_device": settings["device"] if mtp["enabled"] else None,
        "draft_location": redactor.redact(resolve_path(mtp["model"])) if mtp.get("model") else None,
        # A profile's own CACHE_DIR is a local path: redacted like settings.cache_dir.
        "effective_ov": {k: redactor.redact(v) if k == "CACHE_DIR" else v
                         for k, v in profile["ov"].items()},
        "effective_scheduler": dict(profile["scheduler"]),
        "ov_config": format_config({k: v for k, v in profile["ov"].items() if k != "CACHE_DIR"}),
        "scheduler_config": format_config(profile["scheduler"]),
        "warmup": settings["warmup"],
        "planned_iterations": planned["planned_iterations"],
        "planned_probes": planned["planned_probes"],
        "iteration_gap_sec": settings["iteration_gap_sec"],
        "output_tokens": settings["output_tokens"],
        "accuracy_seed": (settings.get("accuracy") or {}).get("seed"),
        "gpu_budget_gb": settings.get("gpu_memory_budget_gb") if is_gpu(settings["device"]) else None,
        "weight_disk_gb": static.get("weight_disk_gb"),
        "status": "running",
        "flags": [],
        "error": None,
        "warnings": [],
        "measurement_errors": [],
        "load_ok": False,
        "iterations": [],
        "_probes": [],
        "_raw_probes": [],
        "_model_dir": str(static.get("model_dir")),
    }


def _case_spec(planned: dict, prepared: dict, settings, specs: list) -> dict:
    """The immutable, serializable mapping a child runs; no runtime objects."""
    mtp = dict(prepared["mtp"])
    if mtp.get("model"):
        mtp["model"] = str(resolve_path(mtp["model"]))
    output_tokens = settings["output_tokens"]
    if specs:
        output_tokens = max(spec.output_tokens for spec in specs)
    return thaw({
        "case_id": planned["case_id"],
        "mode": planned["mode"],
        "model_dir": planned["model_dir"],
        "device": settings["device"],
        "context_tokens": planned["context_tokens"],
        "output_tokens": output_tokens,
        "warmup": settings["warmup"],
        "iterations": settings["iterations"],
        "iteration_gap_sec": settings["iteration_gap_sec"],
        "timeout_sec": settings["timeout_sec"],
        "ov_config": prepared["ov"],
        "scheduler_config": prepared["scheduler"],
        "mtp": mtp,
        "throughput_task": planned["task_framing"],
        "probes": [accuracy.probe_payload(spec, index) for index, spec in enumerate(specs)] or None,
    })


def _comparison_key(case: dict) -> str | None:
    """Everything a matched speculative/plain pair must share, hashed.

    The fully resolved ov/scheduler mappings are included; for auto-sized pools the pool size
    is left out of the key and a differing size marks the pair confounded instead.
    """
    if case.get("mode") != MODE_THROUGHPUT or not case.get("prompt_sha256"):
        return None
    scheduler = dict(case.get("effective_scheduler") or {})
    if case.get("cache_pool") == POOL_AUTO:
        scheduler.pop("cache_size", None)
    payload = {
        key: case.get(key) for key in (
            "model", "artifact_id", "weight_precision", "weight_format", "device",
            "context_tokens", "pipeline_mode", "throughput_task", "task_sha256",
            "prompt_sha256", "prompt_tokens", "warmup", "planned_iterations",
            "iteration_gap_sec", "output_tokens",
        )
    }
    payload.update({"ov": case.get("effective_ov"), "scheduler": scheduler})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _evaluate_status(case: dict) -> tuple:
    """Status in the documented precedence, plus every other condition as a flag.

    Explicit runtime/artifact failure; no measured output; incomplete coverage; unavailable
    required measurement; GPU budget exceeded; host budget exceeded; otherwise ``ok``.
    """
    error = case.get("error") or None
    category = (error or {}).get("category")
    if category in _EXPLICIT_FAILURES:
        explicit = category
    elif category and category != "no_output":
        explicit = "load_error" if (not case.get("load_ok") and error.get("stage") == "load") else "error"
    else:
        explicit = None

    accuracy_mode = case.get("mode") == MODE_ACCURACY
    completed = case.get("completed_probes" if accuracy_mode else "completed_iterations") or 0
    planned = case.get("planned_probes" if accuracy_mode else "planned_iterations") or 0
    # A probe whose prompt could not be built is skipped and accounted for, as the reference
    # does: the case stays `ok` and its rates are over the scored probes. Only probes that
    # neither ran nor were skipped make an accuracy case partial.
    accounted = completed + ((case.get("skipped_probes") or 0) if accuracy_mode else 0)
    conditions = []
    if completed == 0:
        conditions.append("no_output")
    elif accounted < planned:
        conditions.append("partial")
    if case.get("measurement_errors"):
        conditions.append("measurement_error")
    if case.get("gpu_budget_exceeded"):
        conditions.append("gpu_memory_limit")
    if case.get("system_memory_limit_exceeded"):
        conditions.append("memory_limit")
    status = explicit or next((c for c in _DERIVED_STATUSES if c in conditions), "ok")
    flags = [c for c in conditions if c != status]
    if case.get("precision_label_mismatch"):
        flags.append("precision_label_mismatch")
    if case.get("potentially_contaminated"):
        flags.append("potentially_contaminated")
    return status, flags


def _status(case: dict) -> str:
    return _evaluate_status(case)[0]


def _finalize_case(case: dict, settings, specs: list) -> dict:
    """Score (accuracy), aggregate, apply budgets and choose the status. Runs after the
    child has exited and the sampler has stopped."""
    gpu_run = is_gpu(settings["device"])
    if case["mode"] == MODE_ACCURACY:
        rows, summary = accuracy.score_case(case.pop("_raw_probes", []), specs)
        case["_probes"] = rows
        case["accuracy"] = summary
        records = [r for r in rows if r.get("status") == "ok"]
        case["completed_probes"] = len(records)
        case["skipped_probes"] = sum(1 for r in rows if r.get("status") == "skipped")
        aggregated = metrics.aggregate(records)
        # Different probes are not repeated-prompt trials: their consistency is unavailable.
        aggregated.update({"output_consistent": None, "output_sha256": None, "output_text": None})
    else:
        records = metrics.measured(case["iterations"])
        case["completed_iterations"] = len(records)
        aggregated = metrics.aggregate(case["iterations"])
    case.update(aggregated)

    errors = case["measurement_errors"]
    inference_ran = bool(records)
    if inference_ran:
        if case.get("peak_ram_pct") is None:
            errors.append("host RAM telemetry unavailable")
        if gpu_run and case.get("peak_gpu_gb") is None:
            errors.append("GPU memory telemetry unavailable")
        if any(r.get("input_count_mismatch") for r in records):
            errors.append("runtime input count differs from the counted prompt; exact-context "
                          "certification is invalid")
        if any(r.get("peak_ram_gb") is None for r in records if r.get("memory_window") is not None) \
                or any(r.get("memory_window") is None for r in records):
            errors.append("an iteration memory window is unavailable")
    budget = case.get("gpu_budget_gb")
    peak_gpu, mean_gpu = case.get("peak_gpu_gb"), case.get("mean_gpu_gb")
    case["peak_gpu_pct_of_budget"] = (peak_gpu / budget * 100) if budget and peak_gpu is not None else None
    case["mean_gpu_pct_of_budget"] = (mean_gpu / budget * 100) if budget and mean_gpu is not None else None
    ram_pct = case.get("peak_ram_pct")
    case["system_memory_limit_exceeded"] = bool(
        ram_pct is not None and ram_pct > settings["max_system_memory_pct"]
    )
    case["gpu_budget_exceeded"] = bool(gpu_run and budget and peak_gpu is not None and peak_gpu > budget)
    case["prompt_sha256"] = (case.get("prompt") or {}).get("prompt_sha256") \
        if case["mode"] == MODE_THROUGHPUT else None
    case["comparison_key"] = _comparison_key(case)
    case["status"], case["flags"] = _evaluate_status(case)
    return case


def _apply_result(case: dict, result: dict) -> None:
    for key in ("load_ok", "load_time_s", "prompt", "prompt_tokens", "stage_reached", "phase",
                "active_iteration", "error", "cleanup_confirmed",
                "exit_code", "memory_baseline", "peak_ram_gb", "peak_ram_pct",
                "min_available_ram_gb", "peak_gpu_gb", "post_load_peak_ram_gb",
                "post_load_peak_gpu_gb", "memory_samples", "memory_sample_interval_s",
                "gpu_budget_driver_gb", "invalid_events"):
        if key in result:
            case[key] = result[key]
    case["device_diagnostics"] = result.get("device") or {}
    case["iterations"] = list(result.get("iterations") or [])
    case["_raw_probes"] = list(result.get("probes") or [])
    if result.get("invalid_events"):
        case["warnings"].append(f"{result['invalid_events']} invalid child event(s) ignored")


def _run_case(planned: dict, settings, static: dict, prepared, run, contaminated: bool = False) -> dict:
    """One case: preflight result, child execution, parent-side scoring and status."""
    case = _new_case(planned, settings, static, run.redactor)
    case["potentially_contaminated"] = contaminated
    if contaminated:
        case["warnings"].append("memory had not returned to the previous case's baseline; this "
                                "case may be charged with leftover memory")
    specs = (accuracy.build_probe_specs(thaw(settings["accuracy"]), planned["context_tokens"])
             if planned["mode"] == MODE_ACCURACY else [])
    if isinstance(prepared, _Rejected):
        case["error"] = {"category": prepared.category, "stage": "preflight", "milestone": None,
                         "phase": "preflight", "message": run.redactor.text(prepared.message)}
        return _finalize_case(case, settings, specs)

    case.update({
        "effective_ov": dict(prepared["ov"]),
        "effective_scheduler": dict(prepared["scheduler"]),
        "ov_config": format_config({k: v for k, v in prepared["ov"].items() if k != "CACHE_DIR"}),
        "scheduler_config": format_config(prepared["scheduler"]),
        "cache_size_gb": prepared["scheduler"].get("cache_size"),
        "expected_kv_gb": prepared["estimate"]["gib"],
        "cache_estimate": {k: v for k, v in prepared["estimate"].items()},
        "cache_ceiling_gb": prepared["ceiling_gb"],
        "draft_weight_precision": prepared["draft_weight_precision"],
        "draft_artifact_id": (evidence.artifact_identity(str(prepared["draft_dir"]), run.redactor)["artifact_id"]
                              if prepared.get("draft_dir") else None),
    })
    if "CACHE_DIR" in prepared["ov"]:
        cache_path = prepared["ov"]["CACHE_DIR"]
        case["effective_ov"]["CACHE_DIR"] = run.redactor.redact(cache_path)
        case["compiled_cache_preexisting"] = bool(os.path.isdir(cache_path) and os.listdir(cache_path))
    case["warnings"].extend(prepared["warnings"])
    spec = _case_spec(planned, prepared, settings, specs)
    _print_case_banner(case, settings, len(specs))

    def _on_record(kind: str, record: dict) -> None:
        (case["iterations"] if kind == "iteration" else case["_raw_probes"]).append(record)
        _echo_record(kind, record, settings["record_text"])
        run.checkpoint()

    run.active = case
    include_gpu = is_gpu(settings["device"])
    interrupted = None
    try:
        result = _run_case_subprocess(spec, settings["timeout_sec"], include_gpu=include_gpu,
                                      on_record=_on_record)
    except CaseInterrupted as exc:
        result, interrupted = exc.result, exc
    except ChildCleanupError as exc:
        result = exc.result
        run.refuse_more = True
        run.warnings.append(f"{case['case_id']}: child process death could not be confirmed; "
                            "no further case was started")
        result["error"] = {"category": "orchestration", "stage": "cleanup", "milestone": None,
                           "phase": "cleanup", "message": "child did not exit after terminate/kill"}
    except Exception as exc:  # noqa: BLE001 - the orchestrator could not carry the case out
        traceback.print_exc()
        result = {"error": {"category": "orchestration", "stage": "spawn", "milestone": None,
                            "phase": "spawn", "message": run.redactor.text(f"{type(exc).__name__}: {exc}")}}
    finally:
        run.active = None
    _apply_result(case, result)
    if (case.get("device_diagnostics") or {}).get("device_name"):
        names = run.environment["device_names"]
        if case["device_diagnostics"]["device_name"] not in names:
            names.append(case["device_diagnostics"]["device_name"])
    if interrupted is None and result.get("memory_baseline") is not None and result.get("cleanup_confirmed", True):
        try:
            settled = _wait_for_memory_settle(result["memory_baseline"], include_gpu)
        except KeyboardInterrupt:
            # The case itself completed: keep its records, then stop the run.
            interrupted = True
        else:
            case["memory_settled"] = settled
            case["_next_contaminated"] = not settled
            if not settled:
                case["warnings"].append("memory did not return within 1 GiB of the pre-case "
                                        "baseline after 30 s; the next case is flagged")
    _finalize_case(case, settings, specs)
    if interrupted is not None:
        raise CaseInterrupted(case)
    return case


def _print_case_banner(case: dict, settings, probes: int) -> None:
    work = (f"{settings['warmup']} warmup + {probes} accuracy probe(s)" if probes
            else f"{settings['warmup']} warmup + {settings['iterations']} iterations")
    print(f"\n[{case['case_id']} {case['label']}] {work}, progress timeout "
          f"{settings['timeout_sec']:g}s", flush=True)
    print(f"  pipeline: {case['pipeline_mode']} | ov: {case['ov_config']}", flush=True)
    if case["pipeline_mode"] == PIPELINE_PAGED:
        print(f"  scheduler: {case['scheduler_config']} (pool: {case['cache_pool']})", flush=True)
    print(f"  weights: {case.get('weight_precision') or 'unknown'} ({case.get('model_location')})", flush=True)
    if case["mtp"]:
        print(f"  speculative: {case['speculative_strategy']} k={case['num_assistant_tokens']}, "
              f"draft weights {case.get('draft_weight_precision') or 'unknown'}", flush=True)


def _echo_record(kind: str, record: dict, record_text: bool) -> None:
    if record.get("status") == "skipped":
        print(f"  probe {record.get('task')} #{record.get('sample')}: skipped "
              f"({record.get('skip_reason')})", flush=True)
        return
    line = "  " + metrics.format_iteration(record)
    if kind == "probe":
        depth = metrics.finite_number(record.get("depth"))
        where = f" d={depth:.2f}" if depth is not None else ""
        line = f"  probe {record.get('task')}{where} #{record.get('sample')}: " + line.strip()
        if record_text:
            answer = " ".join(str(record.get("prediction_text") or "").split())
            answer = answer.encode("ascii", "backslashreplace").decode("ascii")
            line += f" | answer: {answer[:57] + '...' if len(answer) > 60 else answer}"
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Run state and evidence
# ---------------------------------------------------------------------------
def _answer_encoder(case: dict | None):
    """``text -> ids`` for FDT/SDT: the model's local tokenizer, else labelled words."""
    model_dir = (case or {}).get("_model_dir")
    if model_dir in _ANSWER_ENCODERS:
        return _ANSWER_ENCODERS[model_dir]
    encoder = None
    if model_dir:
        try:
            tokenizer = trial_runner._load_tokenizer(model_dir)

            def encoder(text):
                return list(tokenizer.encode(text or "", add_special_tokens=False))

            encoder.unit = "model tokens"
        except Exception as exc:  # noqa: BLE001 - an optional unit must not end the run
            print(f"  [warn] FDT/SDT fall back to words: tokenizer unavailable "
                  f"({type(exc).__name__})", flush=True)
            encoder = None
    if encoder is None:
        def encoder(text):
            return scoring.lexical_tokens(text)

        encoder.unit = "words"
    _ANSWER_ENCODERS[model_dir] = encoder
    return encoder


class RunState:
    """All run-level state; `checkpoint` persists a snapshot of it atomically."""

    def __init__(self, settings, planned: list, redactor, run_id: str, run_dir: str):
        self.settings = settings
        self.planned = planned
        self.redactor = redactor
        self.run_id = run_id
        self.run_dir = run_dir
        self.cases: list = []
        self.active = None
        self.revision = 0
        self.warnings: list = [redactor.text(n) for n in settings.get("notices") or ()]
        self.started = evidence.utc_now()
        self.ended = None
        self.completed = False
        self.interrupted = False
        self.refuse_more = False
        self.fatal = None
        self.write_failed = False
        self.state = {p["case_id"]: "pending" for p in planned}
        self.environment = _environment()
        self.commit = _tool_commit()
        self.artifacts: dict = {}
        self.similarity_notes: list = []

    # -- export ---------------------------------------------------------------
    def _export_settings(self) -> dict:
        exported = thaw(self.settings)
        redact = self.redactor.redact
        exported["config_path"] = redact(exported.get("config_path"))
        exported["models_base_path"] = redact(resolve_path(exported["models_base_path"]))
        exported["model_dirs"] = {k: redact(resolve_path(v)) for k, v in exported["model_dirs"].items()}
        exported["output_dir"] = redact(resolve_path(exported["output_dir"]))
        if exported.get("cache_dir"):
            exported["cache_dir"] = redact(resolve_path(exported["cache_dir"]))
        for profile in exported["profiles"]:
            if profile["ov"].get("CACHE_DIR"):
                profile["ov"]["CACHE_DIR"] = redact(resolve_path(profile["ov"]["CACHE_DIR"]))
            if profile["mtp"].get("model"):
                profile["mtp"]["model"] = redact(resolve_path(profile["mtp"]["model"]))
        exported["notices"] = [self.redactor.text(n) for n in exported.get("notices") or []]
        return exported

    def _export_case(self, case: dict) -> dict:
        exported = {k: v for k, v in case.items() if not k.startswith("_")}
        exported["probes"] = case.get("_probes") or case.get("_raw_probes") or []
        exported["warnings"] = [self.redactor.text(w) for w in case.get("warnings") or []]
        if isinstance(exported.get("error"), dict) and exported["error"].get("message"):
            exported["error"] = {**exported["error"],
                                 "message": self.redactor.text(exported["error"]["message"])}
        return evidence.to_plain(evidence.strip_text(exported, self.settings["record_text"]))

    def counts(self) -> dict:
        statuses = [c["status"] for c in self.cases]
        return {
            "planned": len(self.planned),
            "processed": len(self.cases),
            "ok": statuses.count("ok"),
            "failed": sum(1 for s in statuses if s != "ok"),
            "partial": statuses.count("partial"),
            "not_run": sum(1 for s in self.state.values() if s != "done"),
            "skipped_matrix_points": len(skipped_matrix_points(self.settings)) * len(self.settings["models"])
                                     * len(self.settings["profiles"]),
            "by_status": {s: statuses.count(s) for s in sorted(set(statuses))},
        }

    def exit_code(self) -> int:
        if self.interrupted:
            return EXIT_INTERRUPTED
        if self.fatal or self.write_failed or self.refuse_more or not self.completed:
            return EXIT_FAILURE
        if any(c["status"] != "ok" or c.get("precision_label_mismatch") for c in self.cases):
            return EXIT_FAILURE
        return EXIT_OK

    def snapshot(self) -> dict:
        return evidence.to_plain({
            "schema_version": evidence.SCHEMA_VERSION,
            "run_id": self.run_id,
            "revision": self.revision,
            "tool": {"name": "context_bench", "commit": self.commit},
            "started_utc": self.started,
            "ended_utc": self.ended,
            "completed": self.completed,
            "interrupted": self.interrupted,
            "exit_code": self.exit_code() if self.ended else None,
            "source_config": {"path": self.redactor.redact(self.settings.get("config_path")),
                              "sha256": self.settings.get("config_sha256")},
            "settings": self._export_settings(),
            "environment": self.environment,
            "artifacts": self.artifacts,
            "statistics": {"percentile_method": metrics.PERCENTILE_METHOD,
                           "small_sample_threshold": metrics.SMALL_SAMPLE_THRESHOLD,
                           "units": {"latency": "ms", "generation_time": "s", "load_time": "s",
                                     "throughput": "tokens/s", "memory": "GiB"}},
            "counts": self.counts(),
            "warnings": self.warnings + ([f"orchestration failure: {self.fatal}"] if self.fatal else []),
            "plan": [{"case_id": p["case_id"], "label": p["label"], "mode": p["mode"],
                      "model": p["model"], "context_tokens": p["context_tokens"],
                      "throughput_task": p["throughput_task"],
                      "task_variant": p.get("task_variant"), "profile": p["profile"]["name"],
                      "planned_iterations": p["planned_iterations"],
                      "planned_probes": p["planned_probes"], "state": self.state[p["case_id"]]}
                     for p in self.planned],
            "cases": [self._export_case(c) for c in self.cases],
            "in_progress": self._export_case(self.active) if self.active else None,
            "similarity_notes": self.similarity_notes,
        })

    # -- persistence ------------------------------------------------------------
    def checkpoint(self, render: bool = False) -> list | None:
        if render and self.settings.get("accuracy"):
            # Every rendered report fills fidelity and similarity, as the reference does, so a
            # run that ends early still reports the similarity of the cases it finished.
            baseline = self.settings["accuracy"].get("baseline_profile")
            self.similarity_notes = accuracy.fill_generation_fidelity(
                self.cases, baseline, _answer_encoder,
                self.settings["accuracy"].get("embedding_model"), with_similarity=True,
            )
            accuracy.fill_deltas(self.cases, baseline)
        self.revision += 1
        snapshot = self.snapshot()
        evidence.atomic_write_json(os.path.join(self.run_dir, evidence.RESULTS_NAME), snapshot)
        if not render:
            return None
        lines = report.render_report(snapshot)
        evidence.atomic_write_text(os.path.join(self.run_dir, evidence.REPORT_NAME),
                                   "\n".join(lines) + "\n")
        return lines

    def finish_case(self, case: dict) -> None:
        self.cases.append(case)
        self.state[case["case_id"]] = "done"
        write_report(self)


def write_report(run: RunState) -> list:
    """Fill cross-case scores, then checkpoint `results.json` and render `report.txt`."""
    return run.checkpoint(render=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def execute(settings, planned: list, redactor) -> int:
    """Run the planned matrix serially; returns the process exit code."""
    for notice in settings.get("notices") or ():
        print(f"[note] {notice}", flush=True)
    output_base = resolve_path(settings["output_dir"])
    try:
        run_id, run_dir = evidence.create_run_dir(str(output_base))
    except OSError as exc:
        print(f"context_bench: cannot create the run directory: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_FAILURE
    run = RunState(settings, planned, redactor, run_id, run_dir)
    print(f"context_bench run {run_id} -> {redactor.redact(run_dir)} ({len(planned)} case(s))", flush=True)

    try:
        run.checkpoint()
        contaminated = False
        for model in settings["models"]:
            model_cases = [p for p in planned if p["model"] == model]
            if not model_cases:
                continue
            static = _model_static(settings, model, Path(model_cases[0]["model_dir"]), redactor)
            run.artifacts[model] = static["identity"]
            if static.get("precision_label_mismatch"):
                run.warnings.append(f"{model}: weight_format label {settings['weight_format']!r} "
                                    f"does not match the IR ({static['weight_precision']}); its "
                                    "cases are not qualified for that precision")
            prepared = {}
            for entry in model_cases:
                try:
                    prepared[entry["case_id"]] = _preflight_case(entry, settings, static)
                except _Rejected as rejected:
                    prepared[entry["case_id"]] = rejected
            for entry in model_cases:
                if run.refuse_more:
                    run.state[entry["case_id"]] = "refused"
                    continue
                try:
                    case = _run_case(entry, settings, static, prepared[entry["case_id"]], run, contaminated)
                except CaseInterrupted as interrupted:
                    run.finish_case(interrupted.result)
                    raise KeyboardInterrupt from None
                if "_next_contaminated" in case:  # a rejected case leaves memory as it was
                    contaminated = bool(case.pop("_next_contaminated"))
                run.finish_case(case)
                print(report.format_case_line(run._export_case(case)), flush=True)
        run.completed = not run.refuse_more
    except KeyboardInterrupt:
        run.interrupted = True
        print("\ncontext_bench: interrupted; child cleaned up, writing evidence", flush=True)
    except Exception as exc:  # noqa: BLE001 - record the orchestration failure, keep evidence
        run.fatal = redactor.text(f"{type(exc).__name__}: {exc}")
        print(trial_runner.sanitize_message(traceback.format_exc(), 4000, keep_newlines=True),
              file=sys.stderr)
    finally:
        for case_id, state in run.state.items():
            if state == "pending":
                run.state[case_id] = "not_run"
        run.ended = evidence.utc_now()
        try:
            lines = write_report(run)
        except Exception as exc:  # noqa: BLE001 - must not mask what is already unwinding
            run.write_failed = True
            print(f"context_bench: writing evidence failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            print("\n" + "\n".join(lines), flush=True)
            print(f"Evidence: {redactor.redact(os.path.join(run_dir, evidence.RESULTS_NAME))} and "
                  f"{evidence.REPORT_NAME} (revision {run.revision})"
                  + ("" if run.completed else " -- run ended early, marked incomplete"), flush=True)
    return run.exit_code()


def main(argv=None) -> int:
    try:
        args = _parse_args(argv)
        settings = _load_settings(args)
        planned = plan_run(settings)
    except ConfigError as exc:
        print(f"context_bench: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    redactor = evidence.PathRedactor(SC_ROOT)
    if args.list_profiles:
        print_plan(settings, planned, redactor.redact)
        return EXIT_OK
    try:
        _preflight_environment_check()
    except DependencyError as exc:
        print(f"context_bench: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return execute(settings, planned, redactor)


if __name__ == "__main__":
    sys.exit(main())

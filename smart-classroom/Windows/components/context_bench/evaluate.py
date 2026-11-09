# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Cross-model evaluation: performance and accuracy of the INT4 text-generation models.

Runs `benchmark` once per (model, suite) with one shared workload, so the models are measured
under identical contexts, tasks, decode ceilings and repetition, then renders every run's
`results.json` into one Markdown report, `evaluation_report.md`.

  Model             Config                      Profiles (every profile of the config)
  qwen3-vl-8b       config_qwen3_8b.yaml        paged_min (plain), eagle3_k3/k4/k5 (Eagle3)
  qwen3.5-9b        config_qwen3.5_9b.yaml      paged_min (plain), dflash_k3/k5/k7/k15 (DFlash)
  qwen3.6-35b-a3b   config_qwen3.6_35b.yaml     paged_min (plain), dflash_k3/k5/k7/k15 (DFlash)
  qwen3.8-27b       config_qwen3.8_27b.yaml     paged_min

  Suite       What each invocation measures
  throughput  code/math/chat at context 0 and 8,000 tokens, summary_2s at 8,000; 512 tokens
  classroom   the application's summarizer request at 8,000 tokens; 1024 tokens
  accuracy    RULER retrieval + generation fidelity probes at 8,000 tokens (--accuracy)

Speculative decoding off and on run in the same invocation, so every DFlash/Eagle3 case has
its matched plain baseline (identical artifact, prompt and scheduler) and the speedup is an
isolated one. `--spec-k K ...` keeps only the speculative profiles with those windows.

    python -m components.context_bench.evaluate --list-profiles      # every plan; loads nothing
    python -m components.context_bench.evaluate                      # the full evaluation
    python -m components.context_bench.evaluate --quick              # smoke test of the pipeline
    python -m components.context_bench.evaluate --report <evaluation dir>   # re-render only

Exit codes: 0 every invocation exited 0; 1 at least one failed, partial or missing case, or a
run that wrote no evidence; 2 invalid arguments; 130 Ctrl-C (the report covers what finished).

See smart-classroom/docs/dev-guide/context-bench/context-bench-guide.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from components.context_bench import accuracy, evidence, metrics, report
from components.context_bench.benchmark import EXIT_CONFIG, EXIT_FAILURE, EXIT_INTERRUPTED, EXIT_OK
from components.context_bench.settings import (
    SC_ROOT,
    SPECULATIVE_STRATEGIES,
    TOOL_DIR,
    context_label,
    load_yaml,
)

MANIFEST_NAME = "evaluation.json"
REPORT_NAME = "evaluation_report.md"
DEFAULT_OUTPUT_DIR = "monitoring/executionlogs/context_bench/evaluation"
BASELINE_PROFILE = "paged_min"
DEFAULT_TIMING_GAP_SEC = 30.0  # the configs' iteration_gap_sec; see default_iteration_gap


@dataclass(frozen=True)
class ModelSpec:
    key: str
    config: str


MODELS = (
    ModelSpec("qwen3-vl-8b", "config_qwen3_8b.yaml"),
    ModelSpec("qwen3.5-9b", "config_qwen3.5_9b.yaml"),
    ModelSpec("qwen3.6-35b-a3b", "config_qwen3.6_35b.yaml"),
    ModelSpec("qwen3.8-27b", "config_qwen3.8_27b.yaml"),
)
MODEL_KEYS = tuple(m.key for m in MODELS)

SUITE_THROUGHPUT = "throughput"
SUITE_CLASSROOM = "classroom"
SUITE_ACCURACY = "accuracy"
SUITES = (SUITE_THROUGHPUT, SUITE_CLASSROOM, SUITE_ACCURACY)

# Per-suite workload: (full, quick). Repetition is added by `suite_arguments`; the classroom
# request needs its full 1024-token ceiling even in quick mode, or it is cut off.
_CLASSROOM = ["--contexts", "8000", "--throughput-task", "classroom_summary", "--output-tokens", "1024"]
_WORKLOADS = {
    SUITE_THROUGHPUT: (
        ["--contexts", "0", "8000", "--throughput-task", "code", "math", "chat", "summary_2s",
         "--output-tokens", "512"],
        ["--contexts", "8000", "--throughput-task", "summary_2s", "--output-tokens", "128"],
    ),
    SUITE_CLASSROOM: (_CLASSROOM, _CLASSROOM),
    SUITE_ACCURACY: (
        ["--accuracy", "--contexts", "8000"],
        ["--accuracy", "--contexts", "8000", "--accuracy-depths", "0.5", "--accuracy-samples", "1"],
    ),
}


class EvaluationError(Exception):
    """Invalid evaluation arguments (exit 2)."""


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def config_profiles(config: str) -> list:
    """``(name, k)`` for every profile of `config`, in config order; k is the speculative
    window (its dflash/eagle3 ``num_assistant_tokens``), None for a plain profile. The
    benchmark validates the profiles themselves when it resolves the invocation."""
    profiles = []
    for entry in load_yaml(TOOL_DIR / config).get("profiles") or []:
        section = next((entry[s] for s in SPECULATIVE_STRATEGIES
                        if isinstance(entry.get(s), dict)), None)
        enabled = section is not None and section.get("enabled", True) is not False
        profiles.append((entry.get("name"),
                         section.get("num_assistant_tokens") if enabled else None))
    return profiles


def select_profiles(config: str, windows=None) -> list:
    """Every profile of `config`; with `windows`, its plain profiles plus the speculative
    profiles whose window is listed."""
    return [name for name, k in config_profiles(config)
            if windows is None or k is None or k in windows]


def default_iteration_gap(suite: str, quick: bool) -> float:
    """Idle before each measured generation when --iteration-gap is not given.

    The timing suites idle like the configs (and the published reference): back to back, the
    iGPU's clocks sag over the iterations, and speculative decoding, which works the GPU harder
    per step, loses more than the plain baseline it is compared against. Accuracy timings are not used,
    and its gap would apply to every probe, so it runs back to back; so does --quick.
    """
    return 0.0 if quick or suite == SUITE_ACCURACY else DEFAULT_TIMING_GAP_SEC


def suite_arguments(suite: str, quick: bool, iteration_gap: float | None) -> list:
    workload = list(_WORKLOADS[suite][1 if quick else 0])
    repeat = ["--warmup", "0", "--iterations", "1"] if quick else ["--warmup", "1", "--iterations", "3"]
    if iteration_gap is None:
        iteration_gap = default_iteration_gap(suite, quick)
    return workload + repeat + ["--iteration-gap", f"{iteration_gap:g}"]


def plan_invocations(models, suites, *, quick: bool = False, windows=None,
                     weight_format: str = "int4", device: str | None = None,
                     iteration_gap: float | None = None) -> list:
    """One benchmark invocation per (model, suite), model by model, each running every
    profile of the model's config (or, with `windows`, the speculative ones of those k)."""
    selected = [m for m in MODELS if m.key in models]
    if windows is not None:
        available = sorted({k for m in selected for _, k in config_profiles(m.config)
                            if k is not None})
        missing = sorted(set(windows) - set(available))
        if missing:
            raise EvaluationError(f"no selected model has a speculative profile for k={missing}; "
                                  f"available windows: {available}")
    invocations = []
    for model in selected:
        profiles = select_profiles(model.config, windows)
        for suite in (s for s in SUITES if s in suites):
            args = ["--config", f"components/context_bench/{model.config}",
                    "--weight-format", weight_format, "--profiles", *profiles]
            if device:
                args += ["--device", device]
            invocations.append({"model": model.key, "suite": suite, "config": model.config,
                                "profiles": profiles,
                                "args": args + suite_arguments(suite, quick, iteration_gap)})
    return invocations


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _benchmark_command(args: list) -> list:
    return [sys.executable, "-m", "components.context_bench.benchmark", *args]


def find_run_dir(output_dir: Path) -> Path | None:
    """The benchmark's run directory inside a fresh per-invocation output directory."""
    if not output_dir.is_dir():
        return None
    runs = sorted(p for p in output_dir.iterdir()
                  if p.is_dir() and (p / evidence.RESULTS_NAME).is_file())
    return runs[-1] if runs else None


def _stream(command: list, log_path: Path) -> int:
    """Run `command` from the Smart Classroom root, teeing its output to `log_path`."""
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    with open(log_path, "w", encoding="utf-8", newline="\n") as log, subprocess.Popen(
        command, cwd=SC_ROOT, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, shell=False,
    ) as child:
        try:
            for raw in child.stdout:
                line = raw.decode("utf-8", errors="replace")
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line.rstrip("\r\n") + "\n")
            return child.wait()
        except KeyboardInterrupt:
            # The console delivered Ctrl-C to the benchmark too: let it clean up its child
            # and checkpoint evidence before this sweep stops.
            return child.wait()


def run_evaluation(invocations: list, evaluation_dir: Path, options: dict) -> int:
    redact = evidence.PathRedactor(SC_ROOT).redact
    manifest = {
        "schema_version": 1,
        "evaluation_id": evaluation_dir.name,
        "started_utc": _utc_now(),
        "ended_utc": None,
        "completed": False,
        "interrupted": False,
        "options": options,
        "runs": [],
    }

    def checkpoint():
        evidence.atomic_write_json(str(evaluation_dir / MANIFEST_NAME), manifest)

    evaluation_dir.mkdir(parents=True, exist_ok=True)
    checkpoint()
    try:
        for number, invocation in enumerate(invocations, 1):
            output_dir = evaluation_dir / invocation["model"] / invocation["suite"]
            output_dir.mkdir(parents=True, exist_ok=True)
            args = invocation["args"] + ["--output-dir", str(output_dir)]
            run = {**invocation, "args": [redact(a) if a == str(output_dir) else a for a in args],
                   "started_utc": _utc_now(), "exit_code": None}
            manifest["runs"].append(run)
            checkpoint()
            print(f"\n=== [{number}/{len(invocations)}] {invocation['model']} / "
                  f"{invocation['suite']} ===", flush=True)
            started = time.monotonic()
            run["exit_code"] = _stream(_benchmark_command(args), output_dir / "console.log")
            run["duration_s"] = round(time.monotonic() - started, 1)
            run["ended_utc"] = _utc_now()
            run_dir = find_run_dir(output_dir)
            run["run_dir"] = redact(run_dir) if run_dir else None
            checkpoint()
            if run["exit_code"] == EXIT_INTERRUPTED:
                raise KeyboardInterrupt
        manifest["completed"] = True
    except KeyboardInterrupt:
        manifest["interrupted"] = True
        print("\nevaluation: interrupted; writing the report for the finished runs", flush=True)
    finally:
        manifest["ended_utc"] = _utc_now()
        checkpoint()
        write_report(evaluation_dir)
    if manifest["interrupted"]:
        return EXIT_INTERRUPTED
    ok = all(r.get("exit_code") == EXIT_OK and r.get("run_dir") for r in manifest["runs"])
    return EXIT_OK if ok else EXIT_FAILURE


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def _cell(text) -> str:
    """One Markdown table cell: single line, pipes escaped."""
    return " ".join(str(text if text is not None else "--").split()).replace("|", "\\|")


def _row(values) -> str:
    return "| " + " | ".join(_cell(v) for v in values) + " |"


def _table(header, rows) -> list:
    return [_row(header), "|" + "---|" * len(header), *(_row(r) for r in rows), ""]


def _fmt(value, spec="{:.1f}") -> str:
    return report._fmt(value, spec)


def _seconds(ms) -> str:
    value = metrics.finite_number(ms)
    return _fmt(value / 1000, "{:.2f}") if value is not None else "--"


def _ratio_cell(value) -> str:
    return f"{value:.2f}x" if value else "--"


def _link(evaluation_dir: Path, path: Path, label: str) -> str:
    relative = Path(os.path.relpath(path, evaluation_dir)).as_posix()
    return f"[{label}]({relative})"


def load_runs(evaluation_dir: Path) -> tuple:
    """The manifest plus, per run, its `results.json` snapshot (None when absent)."""
    manifest = json.loads((evaluation_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    loaded = []
    for run in manifest.get("runs") or []:
        output_dir = evaluation_dir / run["model"] / run["suite"]
        run_dir = find_run_dir(output_dir)
        snapshot = None
        if run_dir is not None:
            snapshot = json.loads((run_dir / evidence.RESULTS_NAME).read_text(encoding="utf-8"))
        loaded.append((run, run_dir, snapshot))
    return manifest, loaded


def _model_order(model: str) -> int:
    return MODEL_KEYS.index(model) if model in MODEL_KEYS else len(MODEL_KEYS)


def _cases(loaded: list, suite: str) -> list:
    """(run, case) pairs of one suite, in model order."""
    pairs = []
    for run, _, snapshot in loaded:
        if run["suite"] == suite and snapshot:
            pairs += [(run, case) for case in snapshot.get("cases") or []]
    return sorted(pairs, key=lambda p: (p[1].get("context_tokens") or 0, p[1].get("throughput_task") or "",
                                        _model_order(p[0]["model"]), p[1].get("case_id") or ""))


def _header_section(manifest: dict, loaded: list, evaluation_dir: Path) -> list:
    options = manifest.get("options") or {}
    snapshot = next((s for _, _, s in loaded if s), None)
    environment = (snapshot or {}).get("environment") or {}
    packages = environment.get("packages") or {}
    state = ("complete" if manifest.get("completed")
             else "interrupted" if manifest.get("interrupted") else "incomplete")
    gap = options.get("iteration_gap")
    gap_label = (f"{gap:g} s" if gap is not None else
                 f"{DEFAULT_TIMING_GAP_SEC:g} s (throughput, classroom), 0 s (accuracy)"
                 if not options.get("quick") else "0 s")
    lines = [
        "# Context Bench Model Evaluation",
        "",
        f"- Evaluation: `{manifest.get('evaluation_id')}` ({state}), started "
        f"{manifest.get('started_utc')}, ended {manifest.get('ended_utc') or '--'}",
        f"- Evidence: `{evidence.PathRedactor(SC_ROOT).redact(evaluation_dir)}`",
        f"- Hardware: {environment.get('processor') or '--'}, {environment.get('memory_gb') or '--'} GiB RAM, "
        f"{', '.join(environment.get('device_names') or []) or '--'}",
        f"- Runtime: openvino {packages.get('openvino') or '--'}, openvino-genai "
        f"{packages.get('openvino-genai') or '--'}, transformers {packages.get('transformers') or '--'}",
        f"- Weights: {options.get('weight_format', '--')} | device: "
        f"{options.get('device') or 'per config (GPU)'} | iteration gap: {gap_label}"
        + (" | speculative windows: " + ", ".join(f"k={k}" for k in options["spec_k"])
           if options.get("spec_k") else ""),
        "",
    ]
    if options.get("quick"):
        lines += ["> **Quick mode**: zero warm-up, one measured iteration and a reduced accuracy "
                  "plan. This checks the pipeline end to end; it is not a repeated-measurement "
                  "result.", ""]
    lines += [
        "Latency in ms, TTFT and generation time in s, throughput in tokens/s, memory in GiB "
        "(system-wide, sampled). `--` is unavailable, never zero. TPOT excludes the first token; "
        "decode tok/s is 1000 / median TPOT. Statistics are medians over the measured iterations. "
        "Results are observations on this machine, not qualification.",
        "",
    ]
    return lines


def _workload_section(loaded: list) -> list:
    rows = []
    for suite in SUITES:
        snapshot = next((s for r, _, s in loaded if r["suite"] == suite and s), None)
        if not snapshot:
            continue
        settings = snapshot.get("settings") or {}
        contexts = ", ".join(context_label(c) for c in settings.get("context_tokens") or [])
        if suite == SUITE_ACCURACY:
            section = settings.get("accuracy") or {}
            tasks = "; ".join(f"{s}: {', '.join((section.get(s) or {}).get('tasks') or [])}"
                              for s in section.get("suites") or [])
            probes = next((p.get("planned_probes") for p in snapshot.get("plan") or []), None)
            repeat = f"{settings.get('warmup')} warm-up + {probes} probes"
            ceiling = "per suite"
        else:
            variants = {p.get("throughput_task"): p.get("task_variant")
                        for p in snapshot.get("plan") or []}
            tasks = ", ".join(f"{t} ({variants[t]})" if variants.get(t) else t
                              for t in settings.get("throughput_tasks") or [])
            repeat = f"{settings.get('warmup')} warm-up + {settings.get('iterations')} measured"
            ceiling = settings.get("output_tokens")
        rows.append([suite, contexts, tasks, ceiling, repeat])
    if not rows:
        return []
    return ["## Workload", "",
            *_table(["Suite", "Contexts", "Tasks", "Output tokens", "Per case"], rows)]


def _runs_section(loaded: list, evaluation_dir: Path) -> list:
    rows = []
    for run, run_dir, snapshot in loaded:
        counts = (snapshot or {}).get("counts") or {}
        statuses = ", ".join(f"{k} {v}" for k, v in (counts.get("by_status") or {}).items())
        evidence_cell = "--"
        if run_dir is not None:
            evidence_cell = (_link(evaluation_dir, run_dir / evidence.REPORT_NAME, "report.txt") + " / "
                             + _link(evaluation_dir, run_dir / evidence.RESULTS_NAME, "results.json"))
        rows.append([run["model"], run["suite"], " ".join(run.get("profiles") or []),
                     run.get("exit_code"),
                     f"{counts.get('ok', 0)}/{counts.get('planned', 0)}" if snapshot else "--",
                     statuses or "--", _fmt(run.get("duration_s"), "{:.0f}") + " s"
                     if run.get("duration_s") is not None else "--", evidence_cell])
    return ["## Runs", "",
            *_table(["Model", "Suite", "Profiles", "Exit", "OK / planned", "Statuses", "Duration",
                     "Evidence"], rows)]


def _performance_section(loaded: list, suite: str, title: str) -> list:
    pairs = [(r, c) for r, c in _cases(loaded, suite) if c.get("mode") == "throughput"]
    if not pairs:
        return []
    rows = [[run["model"], case.get("profile"), context_label(case.get("context_tokens")),
             report.task_label(case), case.get("status"), report._output_tokens_cell(case),
             _seconds(case.get("first_token_latency")), _fmt(case.get("other_tokens_avg_latency")),
             _fmt(case.get("decode_throughput"), "{:.2f}"), _fmt(case.get("prefill_throughput")),
             _fmt(case.get("generation_time"), "{:.2f}"), _fmt(case.get("peak_gpu_gb")),
             _fmt(case.get("peak_ram_gb"))]
            # An absent artifact has no measurement; Runs, the comparison and Failures show it.
            for run, case in pairs if case.get("status") != "missing_ir"]
    lines = [f"## {title}", "",
             *_table(["Model", "Profile", "Context", "Task", "Status", "Output tok", "TTFT s",
                      "TPOT ms", "Decode tok/s", "Prefill tok/s", "Generation s", "GPU peak GiB",
                      "RAM peak GiB"], rows)]
    # Model comparison on the shared baseline profile: decode tok/s (TTFT s) per row.
    models = sorted({r["model"] for r, _ in pairs}, key=_model_order)
    grid: dict = {}
    for run, case in pairs:
        if case.get("profile") == BASELINE_PROFILE:
            key = (case.get("context_tokens") or 0, case.get("throughput_task"))
            grid.setdefault(key, {})[run["model"]] = (
                f"{_fmt(case.get('decode_throughput'), '{:.2f}')} "
                f"({_seconds(case.get('first_token_latency'))})"
                if case.get("status") == "ok" else case.get("status"))
    if len(models) > 1 and grid:
        lines += [f"Model comparison, `{BASELINE_PROFILE}`: decode tok/s (TTFT s); a status "
                  "instead of numbers means the case did not finish `ok`.", "",
                  *_table(["Context", "Task", *models],
                          [[context_label(k[0]), k[1], *(grid[k].get(m, "--") for m in models)]
                           for k in sorted(grid)])]
    return lines


def _speculative_references(loaded: list) -> dict:
    """Each model's `speculative_reference` (published figures) from its snapshots."""
    references = {}
    for run, _, snapshot in loaded:
        reference = ((snapshot or {}).get("settings") or {}).get("speculative_reference")
        if reference:
            references.setdefault(run["model"], reference)
    return references


def _speculative_section(loaded: list) -> list:
    rows = []
    reference_lines = []
    references = _speculative_references(loaded)
    for suite in (SUITE_THROUGHPUT, SUITE_CLASSROOM):
        pairs = _cases(loaded, suite)
        for model in sorted({r["model"] for r, _ in pairs}, key=_model_order):
            cases = [c for r, c in pairs if r["model"] == model]
            for context in sorted({c.get("context_tokens") or 0 for c in cases}):
                at_context = [c for c in cases if (c.get("context_tokens") or 0) == context]
                sweep = report._window_sweep_rows(at_context)
                lines = report._reference_lines(sweep, references.get(model))
                if lines:
                    reference_lines += [f"### {model}, {context_label(context)}", "", *lines]
                for entry in sweep:
                    case = entry["case"]
                    plain = report._matched_baseline(case, report._leaderboard(at_context)) or {}
                    acceptance = metrics.finite_number(case.get("mtp_acceptance_rate"))
                    # A missing hash (repeated outputs disagreed, or no baseline) is unknown,
                    # not a mismatch.
                    ours, theirs = case.get("output_sha256"), plain.get("output_sha256")
                    output = ("--" if not (ours and theirs)
                              else "identical" if ours == theirs else "differs")
                    rows.append([
                        model, suite, context_label(context), report.task_label(case),
                        metrics.strategy_label(case), case.get("num_assistant_tokens"),
                        _fmt(plain.get("other_tokens_avg_latency")),
                        _fmt(case.get("other_tokens_avg_latency")),
                        _ratio_cell(entry["decode_speedup"])
                        + (" (confounded)" if entry["confounded"] else ""),
                        _ratio_cell(entry["total_speedup"]),
                        _fmt(case.get("tokens_per_step"), "{:.2f}"),
                        (f"{acceptance * 100:.0f}%" + (" est." if case.get("mtp_acceptance_estimated") else ""))
                        if acceptance is not None else "--",
                        _ratio_cell(entry["pass_cost"]),
                        f"{_seconds(plain.get('first_token_latency'))} / "
                        f"{_seconds(case.get('first_token_latency'))}",
                        output,
                    ])
    if not rows:
        return []
    return [
        "## Speculative decoding: off vs on", "",
        "Each DFlash/Eagle3 case is compared only with the `paged_min` case of the same invocation that "
        "shares artifact, prompt, task, device and scheduler, so the drafter is the only change. "
        "Speedup ~= AL / pass cost: AL (acceptance length) is tokens committed per verification "
        "pass, pass cost is one verification pass relative to one plain decode step. Greedy "
        "speculative decoding should reproduce the plain output; `differs` is worth inspecting "
        "in the run's report.txt, and `--` means a side's repeated outputs disagreed, so no "
        "comparison was made.", "",
        *_table(["Model", "Suite", "Context", "Task", "Method", "k", "Plain TPOT ms",
                 "Speculative TPOT ms",
                 "Decode speedup", "Total-call speedup", "AL", "Accepted", "Pass cost",
                 "TTFT s off / on", "Output"], rows),
        *(["## Speculative decoding against the published reference", "",
           "The reference measured whole datasets without a long-context prefix, so the "
           "no-transcript rows are its comparable setting; one prompt per domain here, so "
           "compare ordering and size, not the second decimal.", "", *reference_lines]
          if reference_lines else []),
    ]


def _accuracy_section(loaded: list) -> list:
    pairs = [(r, c) for r, c in _cases(loaded, SUITE_ACCURACY) if c.get("mode") == "accuracy"]
    if not pairs:
        return []
    snapshot = next(s for r, _, s in loaded if r["suite"] == SUITE_ACCURACY and s)
    task_names = list(((snapshot.get("settings") or {}).get("accuracy") or {})
                      .get(accuracy.SUITE_RETRIEVAL, {}).get("tasks") or [])
    lines = ["## Accuracy", ""]
    retrieval = []
    generation = []
    for run, case in pairs:
        r = report._suite_summary(case, accuracy.SUITE_RETRIEVAL)
        g = report._suite_summary(case, accuracy.SUITE_GENERATION)
        context = context_label(case.get("context_tokens"))
        baseline = case.get("profile") == BASELINE_PROFILE
        if r:
            per_task = r.get("per_task") or {}
            retrieval.append([run["model"], case.get("profile"), context, case.get("status"),
                              *(report._pct((per_task.get(t) or {}).get("recall_rate")) for t in task_names),
                              report._pct(r.get("recall_rate")), report._pct(r.get("exact_match_rate")),
                              report._pct(r.get("token_f1")), report._pct(r.get("distractor_rate")),
                              "baseline" if baseline
                              else report._pp((r.get("deltas") or {}).get("recall_rate_delta")),
                              r.get("probe_count")])
        if g:
            generation.append([run["model"], case.get("profile"), context, case.get("status"),
                               report._pct(g.get("fact_coverage")),
                               *(("baseline",) * 4 if baseline else (
                                   report._num(g.get("similarity"), "{:.4f}"),
                                   report._num(g.get("rouge_l")), report._num(g.get("chrf")),
                                   report._pct(g.get("identical_rate")))),
                               g.get("probe_count")])
    if retrieval:
        lines += [
            "### Retrieval (synthetic RULER)", "",
            "Per-task cells are each task's RULER score (share of planted items found; "
            "intersection-over-union for cwe/fwe). Decoys is the share of planted decoys that "
            f"wrongly appeared (lower is better). Delta is the overall recall gap to the same "
            f"model's `{BASELINE_PROFILE}`. Synthetic scores, not published RULER results.", "",
            *_table(["Model", "Profile", "Context", "Status", *task_names, "Overall", "EM", "F1",
                     "Decoys", f"Delta vs {BASELINE_PROFILE}", "Probes"], retrieval),
        ]
    if generation:
        lines += [
            "### Generation (summary, fact sheet)", "",
            "Coverage is scored against the planted facts (ground truth) and compares models. "
            "Similarity (embedding cosine), ROUGE-L, chrF and Identical measure drift from the "
            f"same model's `{BASELINE_PROFILE}` answer to the identical prompt, so they are only "
            "meaningful for a non-baseline profile (here: DFlash/Eagle3).", "",
            *_table(["Model", "Profile", "Context", "Status", "Fact coverage", "Similarity",
                     "ROUGE-L", "chrF", "Identical", "Probes"], generation),
        ]
    notes = sorted({n for _, _, s in loaded if s for n in s.get("similarity_notes") or []})
    lines += [f"> Similarity not measured: {n}" for n in notes] + ([""] if notes else [])
    return lines


def _failure_section(loaded: list) -> list:
    lines = []
    for run, run_dir, snapshot in loaded:
        where = f"{run['model']} / {run['suite']}"
        if snapshot is None:
            lines.append(f"- {where}: no evidence written (exit code {run.get('exit_code')}); "
                         f"see `{run['model']}/{run['suite']}/console.log`")
            continue
        for case in snapshot.get("cases") or []:
            if case.get("status") != "ok":
                detail = report._error_text(case) or "; ".join(case.get("measurement_errors") or [])
                lines.append(f"- {where} {case.get('case_id')} ({case.get('label')}): "
                             f"**{case.get('status')}** {report._one_line(detail, 300)}".rstrip())
        for entry in snapshot.get("plan") or []:
            if entry.get("state") != "done":
                lines.append(f"- {where} {entry.get('case_id')} ({entry.get('label')}): "
                             f"not run ({entry.get('state')})")
        for warning in snapshot.get("warnings") or []:
            if not warning.startswith("smoke test"):
                lines.append(f"- {where}: warning: {report._one_line(warning, 300)}")
    if not lines:
        return []
    return ["## Failures and warnings", "", *lines, ""]


def render_markdown(manifest: dict, loaded: list, evaluation_dir: Path) -> str:
    lines = _header_section(manifest, loaded, evaluation_dir)
    lines += _workload_section(loaded)
    lines += _runs_section(loaded, evaluation_dir)
    lines += _performance_section(loaded, SUITE_THROUGHPUT, "Performance: throughput tasks")
    lines += _performance_section(loaded, SUITE_CLASSROOM, "Performance: classroom summary")
    lines += _speculative_section(loaded)
    lines += _accuracy_section(loaded)
    lines += _failure_section(loaded)
    command = (manifest.get("options") or {}).get("command")
    if command:
        lines += ["## Reproduce", "", "```powershell", command, "```", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def write_report(evaluation_dir: Path) -> Path:
    manifest, loaded = load_runs(evaluation_dir)
    path = evaluation_dir / REPORT_NAME
    evidence.atomic_write_text(str(path), render_markdown(manifest, loaded, evaluation_dir))
    print(f"Evaluation report: {evidence.PathRedactor(SC_ROOT).redact(path)}", flush=True)
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m components.context_bench.evaluate",
        description="Evaluate the INT4 text-generation models with context_bench and write one "
                    "Markdown report.")
    parser.add_argument("--models", nargs="+", choices=MODEL_KEYS, default=list(MODEL_KEYS),
                        help="models to evaluate (default: all)")
    parser.add_argument("--suites", nargs="+", choices=SUITES, default=list(SUITES),
                        help="suites to run per model (default: all)")
    parser.add_argument("--spec-k", type=int, nargs="+", default=None, metavar="K",
                        help="run only the speculative (DFlash/Eagle3) profiles with these "
                        "windows, plus every plain profile (default: every profile of each "
                        "config)")
    parser.add_argument("--weight-format", default="int4", help="weights of every model (default int4)")
    parser.add_argument("--device", help="override every config's device (CPU, GPU, GPU.1)")
    parser.add_argument("--iteration-gap", type=float, default=None, metavar="SECONDS",
                        help="untimed idle before each measured generation, every suite "
                        f"(default {DEFAULT_TIMING_GAP_SEC:g} s for throughput/classroom, 0 for "
                        "accuracy and --quick; 0 measures sustained load)")
    parser.add_argument("--quick", action="store_true",
                        help="smoke test: no warm-up, one iteration, reduced accuracy plan")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"evaluation base directory (default {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--list-profiles", action="store_true",
                        help="print every invocation's plan (benchmark --list-profiles); loads nothing")
    parser.add_argument("--report", metavar="EVALUATION_DIR",
                        help="only re-render evaluation_report.md from an existing evaluation")
    return parser.parse_args(argv)


def _create_evaluation_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = base / stamp
    suffix = 1
    while path.exists():
        suffix += 1
        path = base / f"{stamp}-{suffix}"
    path.mkdir()
    return path


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = _parse_args(argv)
    if args.report:
        evaluation_dir = Path(args.report).resolve()
        if not (evaluation_dir / MANIFEST_NAME).is_file():
            print(f"evaluation: no {MANIFEST_NAME} in {args.report}", file=sys.stderr)
            return EXIT_CONFIG
        write_report(evaluation_dir)
        return EXIT_OK
    if args.iteration_gap is not None and args.iteration_gap < 0:
        print("evaluation: --iteration-gap must be >= 0", file=sys.stderr)
        return EXIT_CONFIG
    try:
        invocations = plan_invocations(
            args.models, args.suites, quick=args.quick, windows=args.spec_k,
            weight_format=args.weight_format, device=args.device, iteration_gap=args.iteration_gap)
    except EvaluationError as exc:
        print(f"evaluation: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.list_profiles:
        worst = EXIT_OK
        for invocation in invocations:
            print(f"\n=== {invocation['model']} / {invocation['suite']} ===", flush=True)
            code = subprocess.run(_benchmark_command(invocation["args"] + ["--list-profiles"]),
                                  cwd=SC_ROOT, shell=False).returncode
            worst = max(worst, code)
        return worst

    base = Path(args.output_dir)
    evaluation_dir = _create_evaluation_dir(base if base.is_absolute() else SC_ROOT / base)
    options = {
        "models": args.models, "suites": args.suites,
        "spec_k": args.spec_k,
        "weight_format": args.weight_format, "device": args.device,
        "iteration_gap": args.iteration_gap, "quick": args.quick,
        "command": " ".join([".\\components\\context_bench\\run_evaluation.ps1",
                             *(argv if argv is not None else sys.argv[1:])]),
    }
    print(f"evaluation {evaluation_dir.name}: {len(invocations)} benchmark invocation(s) -> "
          f"{evidence.PathRedactor(SC_ROOT).redact(evaluation_dir)}", flush=True)
    return run_evaluation(invocations, evaluation_dir, options)


if __name__ == "__main__":
    sys.exit(main())

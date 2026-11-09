# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Render `report.txt` from the same snapshot that is persisted as `results.json`.

Pure functions of plain data: no runtime, no I/O. Sections follow the user guide's
organization -- run header, SPEED AND RESOURCES, SPECULATIVE WINDOW SWEEP, RETRIEVAL
ACCURACY, GENERATION FIDELITY, PROBE DETAIL, failures and warnings. Unavailable values
render as ``--``; failed and budget-exceeded cases stay visible but are never ranked.
Answer and truth text appear only when the run recorded text (`record_text: true`).
"""

from __future__ import annotations

import math

from components.context_bench import accuracy, metrics, settings as plan_settings, tasks
from components.context_bench.settings import POOL_AUTO, context_label

_USABLE_STATUSES = ("ok", "memory_limit", "gpu_memory_limit", "measurement_error", "partial")

# Largest window worth suggesting per strategy: DFlash's block_size 16 minus the seed token;
# the top of serving's qualified Eagle3 range (text_gen_profiles).
_WINDOW_SUGGESTION_CEILING = {"dflash": 15, "eagle3": 8}
_WINDOW_SLACK = 2.0
_FULL_WINDOW_FILL = 0.8

_ACCEPTANCE_LENGTH_LEGEND = (
    "AL = acceptance length: tokens committed per target verification pass, excluding "
    "prefill (accepted draft tokens + the target's own bonus token; 1.00 = no speculation). "
    "Speedup ~= AL / pass cost, so rank windows by AL against what each pass costs -- see "
    "the window sweep -- not by the accepted %, which falls with k by construction."
)
_MEMORY_LEGEND = (
    "Memory is system-wide (other processes count) and sampled; peaks cover load, warm-up "
    "and all iterations, means are per measured window (median), and windows include "
    "IPC/sampling delay. RAM and shared GPU memory overlap and are never added."
)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
# The console formatter, shared so the live log and the report render a value identically.
_fmt = metrics._f


def _ttft_seconds(case: dict) -> str:
    ttft = metrics.finite_number(case.get("first_token_latency"))
    return _fmt(ttft / 1000) if ttft is not None else "--"


def _pct(value) -> str:
    value = metrics.finite_number(value)
    return f"{value * 100:.0f}%" if value is not None else "--"


def _pp(value) -> str:
    value = metrics.finite_number(value)
    return f"{value * 100:+.0f}pp" if value is not None else "--"


def _num(value, spec="{:.2f}") -> str:
    return _fmt(value, spec)


def _one_line(text, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    flat = flat.encode("ascii", "backslashreplace").decode("ascii")
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _heading(text: str) -> list:
    return ["-" * 78, f" {text}", "-" * 78, ""]


def _output_tokens_cell(case: dict) -> str:
    smallest = case.get("output_size_min", case.get("output_size"))
    largest = case.get("output_size_max", case.get("output_size"))
    if smallest != largest:
        return f"{_fmt(smallest, '{:g}')}-{_fmt(largest, '{:g}')}"
    return _fmt(largest, "{:g}")


def _error_text(case: dict) -> str:
    error = case.get("error")
    if not error:
        return ""
    if isinstance(error, dict):
        parts = [error.get("category") or "error"]
        if error.get("stage"):
            parts.append(f"in {error['stage']}")
        if error.get("phase"):
            parts.append(f"(phase {error['phase']})")
        text = " ".join(parts)
        if error.get("message"):
            text += f": {error['message']}"
        if error.get("exit_code") not in (None, 0):
            text += f" [exit code {error['exit_code']}]"
        return text
    return str(error)


# ---------------------------------------------------------------------------
# Speculative cells and hints
# ---------------------------------------------------------------------------
def _mtp_cell(case: dict, prefix: str = "", empty: str = "") -> str:
    if not case.get("mtp"):
        return empty
    parts = [metrics.strategy_label(case), f"k={case.get('num_assistant_tokens')}"]
    if case.get("draft_weight_precision"):
        parts.append("draft " + " ".join(case["draft_weight_precision"].split()[:2]))
    tokens_per_step = metrics.finite_number(case.get("tokens_per_step"))
    if tokens_per_step is not None:
        parts.append(f"AL {tokens_per_step:.2f}")
    acceptance = metrics.finite_number(case.get("mtp_acceptance_rate"))
    if acceptance is not None:
        qualifier = " estimated" if case.get("mtp_acceptance_estimated") else ""
        parts.append(f"{acceptance * 100:.0f}% accepted{qualifier}")
    ratio = metrics.finite_number(case.get("mtp_draft_to_main_ratio"))
    if ratio is not None:
        parts.append(f"draft/main {ratio:.2f}x")
    return prefix + ", ".join(parts) + " "


def _mtp_tuning_hint(case: dict) -> str | None:
    """The next speculative window worth measuring, in either direction; never a
    correctness threshold."""
    tokens = case.get("num_assistant_tokens")
    al = metrics.positive_number(case.get("tokens_per_step"))
    if not case.get("mtp") or not isinstance(tokens, int) or al is None:
        return None
    label = metrics.strategy_label(case)
    if tokens > 1 and tokens + 1 > _WINDOW_SLACK * al:
        smaller = max(1, min(tokens - 1, math.ceil(al)))
        return (
            f"  hint: {label} k={tokens} reached acceptance length {al:.2f}: the target verifies "
            f"{tokens + 1} tokens per pass to commit {al:.1f}. Compare k={smaller} and pick the "
            "window from the sweep table (speedup ~= AL / pass cost)."
        )
    ceiling = _WINDOW_SUGGESTION_CEILING.get(case.get("speculative_strategy"), 15)
    if (al - 1) / tokens >= _FULL_WINDOW_FILL and tokens < ceiling:
        larger = min(ceiling, 2 * tokens)
        return (
            f"  hint: {label} k={tokens} reached acceptance length {al:.2f} of a possible "
            f"{tokens + 1}; the window is nearly full, so compare k={larger}; check the "
            "verification cost in the sweep table."
        )
    return None


# ---------------------------------------------------------------------------
# Ranking and matching
# ---------------------------------------------------------------------------
def _leaderboard(cases: list) -> list:
    """Complete, budget-measured `ok` cases with valid TPOT, by median TPOT then TTFT."""
    return sorted(
        (c for c in cases
         if c.get("status") == "ok" and metrics.positive_number(c.get("other_tokens_avg_latency"))),
        key=lambda c: (
            c["other_tokens_avg_latency"],
            metrics.positive_number(c.get("first_token_latency")) or float("inf"),
        ),
    )


def _ttft_leaderboard(cases: list) -> list:
    return sorted(
        (c for c in cases
         if c.get("status") == "ok" and metrics.positive_number(c.get("first_token_latency"))),
        key=lambda c: c["first_token_latency"],
    )


def _matched_baseline(candidate: dict, cases: list) -> dict | None:
    """A plain case with the identical comparison key (artifact, prompt, device, task,
    plan and fully resolved ov/scheduler); the speculative mechanism is the only change."""
    key = candidate.get("comparison_key")
    if not key:
        return None
    return next((case for case in cases if not case.get("mtp")
                 and case.get("comparison_key") == key), None)


def _confounded(candidate: dict, baseline: dict) -> bool:
    """Auto sizing gave the two cases different pools: not an isolated speculative speedup."""
    auto = POOL_AUTO in (candidate.get("cache_pool"), baseline.get("cache_pool"))
    return auto and candidate.get("cache_size_gb") != baseline.get("cache_size_gb")


def _ratio(numerator, denominator) -> float | None:
    numerator = metrics.positive_number(numerator)
    denominator = metrics.positive_number(denominator)
    return numerator / denominator if numerator and denominator else None


def _pass_cost(case: dict, baseline: dict) -> tuple | None:
    pass_ms, step_ms = case.get("steady_pass_ms"), baseline.get("steady_pass_ms")
    if metrics.positive_number(pass_ms) and metrics.positive_number(step_ms):
        return pass_ms, step_ms
    tokens_per_step = metrics.positive_number(case.get("tokens_per_step"))
    tpot = metrics.positive_number(case.get("other_tokens_avg_latency"))
    step_ms = metrics.positive_number(baseline.get("other_tokens_avg_latency"))
    if tokens_per_step is None or tpot is None or step_ms is None:
        return None
    return tpot * tokens_per_step, step_ms


def _mtp_speedup(cases: list) -> str | None:
    ranked = _leaderboard(cases)
    pair = next(((case, baseline) for case in ranked if case.get("mtp")
                 if (baseline := _matched_baseline(case, ranked)) is not None), None)
    if pair is None:
        return None
    spec, plain = pair
    decode = _ratio(plain["other_tokens_avg_latency"], spec["other_tokens_avg_latency"])
    total = _ratio(plain.get("latency"), spec.get("latency"))
    steady = _ratio(plain.get("steady_tpot"), spec.get("steady_tpot"))
    line = (
        f"{metrics.strategy_label(spec)} decode speedup {decode:.2f}x -- `{spec['profile']}` "
        f"(k={spec.get('num_assistant_tokens')}) at {_fmt(spec['other_tokens_avg_latency'])} "
        f"ms/token against `{plain['profile']}` at {_fmt(plain['other_tokens_avg_latency'])} ms/token"
    )
    if total:
        line += f"; total-call speedup {total:.2f}x (generation time per output token)"
    if metrics.finite_number(spec.get("tokens_per_step")) is not None:
        line += f", acceptance length {spec['tokens_per_step']:.2f} tokens per verification pass"
    acceptance = metrics.finite_number(spec.get("mtp_acceptance_rate"))
    if acceptance is not None:
        qualifier = "estimated " if spec.get("mtp_acceptance_estimated") else ""
        line += f" ({qualifier}{acceptance * 100:.0f}% of drafted candidates accepted)"
    cost = _pass_cost(spec, plain)
    if cost is not None:
        line += (f"; each verification pass took {_fmt(cost[0])} ms, "
                 f"{cost[0] / cost[1]:.2f}x a baseline decode step")
    line += f". TTFT {_ttft_seconds(spec)}s vs {_ttft_seconds(plain)}s."
    if steady:
        firsts = (spec.get("first_decode_ms"), plain.get("first_decode_ms"))
        first = (f" ({_fmt(firsts[0], '{:.0f}')} vs {_fmt(firsts[1], '{:.0f}')} ms)"
                 if all(metrics.positive_number(v) for v in firsts) else "")
        line += (f" Steady state excluding the first decode pass{first}: "
                 f"{_fmt(spec['steady_tpot'])} vs {_fmt(plain['steady_tpot'])} ms/token, "
                 f"{steady:.2f}x.")
    if _confounded(spec, plain):
        line += (" CONFOUNDED: auto sizing produced different cache pools; rerun with an "
                 "explicit matched cache_size for an isolated speedup.")
    return line


def _fork_detail(case: dict, baseline: dict, width: int = 40) -> str | None:
    ours, theirs = case.get("output_text"), baseline.get("output_text")
    if not isinstance(ours, str) or not isinstance(theirs, str):
        return None
    at = next((i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b),
              min(len(ours), len(theirs)))
    return (
        f"  `{case['profile']}` forks at character {at:,} of {len(theirs):,}: after "
        f"{ascii(theirs[max(0, at - width):at])}, baseline {ascii(theirs[at:at + width])} "
        f"vs {ascii(ours[at:at + width])}"
    )


def _mtp_output_check(cases: list) -> str | None:
    inconsistent = [c["profile"] for c in cases
                    if c.get("status") == "ok" and c.get("output_consistent") is False]
    if inconsistent:
        return (
            "WARNING: repeated greedy outputs differ within profile(s) "
            + ", ".join(f"`{name}`" for name in inconsistent)
            + ". The run is nondeterministic, so its speculative output comparison is inconclusive."
        )
    usable = [c for c in cases if c.get("status") == "ok" and c.get("output_sha256")]
    pairs = [(c, b) for c in usable if c.get("mtp")
             if (b := _matched_baseline(c, usable)) is not None]
    if not pairs:
        return None
    mismatches = [(c, b) for c, b in pairs if c["output_sha256"] != b["output_sha256"]]
    if mismatches:
        details = [d for c, b in mismatches if (d := _fork_detail(c, b))]
        return (
            "WARNING: greedy speculative output differs from the baseline for "
            + ", ".join(f"`{c['profile']}`" for c, _ in mismatches)
            + ". Greedy speculative decoding is lossless only up to floating point: verification "
            "scores k + 1 tokens on a different kernel path, so a near-tie can flip. A numerical "
            "speedup with differing outputs is an observation, not a lossless-acceleration "
            "qualification."
            + ("".join("\n" + d for d in details) if details else
               " Set benchmark.record_text: true to show where each output forks.")
        )
    return "Greedy output check: all matched speculative profiles match their baseline exactly."


def task_label(case: dict) -> str | None:
    """The case's task, with the application settings it was resolved from if any."""
    task, variant = case.get("throughput_task"), case.get("task_variant")
    return f"{task} ({variant})" if task and variant else task


def _task_groups(cases: list) -> list:
    groups: dict = {}
    for case in cases:
        groups.setdefault(case.get("throughput_task"), []).append(case)
    return list(groups.items())


def _window_sweep_rows(at_context: list) -> list:
    """One row per speculative throughput case with a matched baseline (accuracy cases pool
    probes of different lengths and are excluded)."""
    rows = []
    for task, group in _task_groups(at_context):
        if task is None:
            continue
        ranked = _leaderboard(group)
        for case in ranked:
            baseline = _matched_baseline(case, ranked) if case.get("mtp") else None
            if baseline is None:
                continue
            cost = _pass_cost(case, baseline)
            rows.append({
                "task": task,
                "case": case,
                "pass_ms": cost[0] if cost else None,
                "pass_cost": _ratio(*cost) if cost else None,
                "decode_speedup": _ratio(baseline.get("other_tokens_avg_latency"),
                                         case.get("other_tokens_avg_latency")),
                "total_speedup": _ratio(baseline.get("latency"), case.get("latency")),
                "steady_speedup": _ratio(baseline.get("steady_tpot"), case.get("steady_tpot")),
                "confounded": _confounded(case, baseline),
            })
    rows.sort(key=lambda row: (row["task"], row["case"].get("num_assistant_tokens") or 0))
    return rows


def _window_sweep_section(at_context: list, context: int, reference: dict | None = None) -> list:
    rows = _window_sweep_rows(at_context)
    if not rows:
        return []
    lines = _heading(f"{context_label(context)} -- SPECULATIVE WINDOW SWEEP (acceptance length vs pass cost)")
    lines += [
        "| Task | Profile | k | AL tok/pass | Accepted source | Accepted | Pass ms | Pass cost (= break-even AL) "
        "| Decode speedup | Total-call speedup | Steady speedup |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        case = row["case"]
        acceptance = metrics.finite_number(case.get("mtp_acceptance_rate"))
        source = "estimated" if case.get("mtp_acceptance_estimated") else (
            "runtime" if acceptance is not None else "--")
        lines.append(
            f"| {row['task']} | {case['profile']} | {case.get('num_assistant_tokens')} "
            f"| {_fmt(case.get('tokens_per_step'), '{:.2f}')} | {source} "
            f"| {_fmt(acceptance * 100 if acceptance is not None else None, '{:.0f}%')} "
            f"| {_fmt(row['pass_ms'])} | {_fmt(row['pass_cost'], '{:.2f}x')} "
            f"| {_fmt(row['decode_speedup'], '{:.2f}x')} | {_fmt(row['total_speedup'], '{:.2f}x')} "
            f"| {_fmt(row['steady_speedup'], '{:.2f}x')}{' (confounded)' if row['confounded'] else ''} |"
        )
    lines.append("")

    def _speedup(row):
        return row["steady_speedup"] or row["decode_speedup"] or 0.0

    best_by_task: dict = {}
    for row in rows:
        current = best_by_task.get(row["task"])
        if current is None or _speedup(row) > _speedup(current):
            best_by_task[row["task"]] = row
    for task, row in best_by_task.items():
        case = row["case"]
        lines.append(
            f"Best window for `{task}`: {case['profile']} (k={case.get('num_assistant_tokens')}) "
            f"-- AL {_fmt(case.get('tokens_per_step'), '{:.2f}')} against a "
            f"{_fmt(row['pass_cost'], '{:.2f}x')} pass, {_fmt(row['steady_speedup'], '{:.2f}x')} "
            f"steady state, {_fmt(row['decode_speedup'], '{:.2f}x')} decode"
        )
    if len(best_by_task) > 1:
        ordered = sorted(best_by_task.items(), key=lambda item: _speedup(item[1]))
        (low_task, low), (high_task, high) = ordered[0], ordered[-1]
        lines.append(
            f"Domain spread: {_speedup(low):.2f}x (`{low_task}`) to {_speedup(high):.2f}x "
            f"(`{high_task}`) with the same draft, target and settings."
        )
    lines.append("")
    lines += _reference_lines(rows, reference)
    return lines


def _reference_lines(rows: list, reference: dict | None) -> list:
    """Published figures at the reference's k, displayed separately from local samples."""
    if not reference:
        return []
    k = reference["num_assistant_tokens"]
    at_k = {row["task"]: row for row in rows if row["case"].get("num_assistant_tokens") == k}
    ran = {row["task"] for row in rows}
    names = [task for task in reference["tasks"] if task in ran]
    if not names:
        return []
    lines = [
        f"Published reference at k={k} (display only; never ranked): {reference['source']}",
        "",
        "| Task | Reference dataset | AL ref | AL measured | Speedup ref | Speedup measured "
        "| Steady speedup measured | Decode tok/s ref | Decode tok/s measured |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for task in names:
        ref, row = reference["tasks"][task], at_k.get(task)
        case = row["case"] if row else {}
        lines.append(
            f"| {task} | {ref['dataset']} | {ref['acceptance_length']:.2f} "
            f"| {_fmt(case.get('tokens_per_step'), '{:.2f}')} | {ref['speedup']:.2f}x "
            f"| {_fmt(row['decode_speedup'] if row else None, '{:.2f}x')} "
            f"| {_fmt(row['steady_speedup'] if row else None, '{:.2f}x')} "
            f"| {_fmt(ref.get('throughput'))} | {_fmt(case.get('decode_throughput'))} |"
        )
    missing = [task for task in names if task not in at_k]
    if missing:
        # A blank line first: Markdown would otherwise read the sentence as a table row.
        lines += ["", f"No k={k} profile ran for {', '.join(missing)}; add one to compare."]
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Speed section
# ---------------------------------------------------------------------------
def _speed_section(at_context: list, context: int, settings: dict, task: str | None = None) -> list:
    budget = settings.get("gpu_memory_budget_gb")
    lines = _heading(
        f"{context_label(context)} -- " + (f"`{task}` -- " if task else "") + "SPEED AND RESOURCES"
    )
    budget_label = f"% of {budget:g} GiB" if metrics.finite_number(budget) else "% of budget"
    lines += [
        "| Model | Profile | Weights | Pipeline | KV | Speculative | Status | n | Output tok "
        "| TPOT ms/tok p50 (p95) | TTFT s p50 | Decode tok/s | E2E tok/s | Prefill tok/s "
        f"| RAM peak/mean GiB | GPU peak/mean GiB ({budget_label}) | KV est GiB | cache |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    ranked = _leaderboard(at_context)
    ranked_ids = {id(c) for c in ranked}
    for case in ranked + [c for c in at_context if id(c) not in ranked_ids]:
        timings = [
            f"{_fmt(case.get('other_tokens_avg_latency'))} ({_fmt(case.get('other_tokens_avg_latency_p95'))})",
            _ttft_seconds(case),
            _fmt(case.get("decode_throughput"), "{:.2f}"),
            _fmt(case.get("e2e_throughput")),
            _fmt(case.get("prefill_throughput")),
        ]
        if case.get("status") not in _USABLE_STATUSES:
            timings = ["--"] * len(timings)
        pool = case.get("cache_pool")
        cache = _fmt(case.get("cache_size_gb"), "{:g}") if pool in ("fixed", "auto") else (
            "runtime" if pool == "runtime" else "--")
        if pool == "auto":
            cache += " (auto)"
        lines.append(
            f"| {case.get('model')} | {case.get('profile')} "
            f"| {case.get('weight_precision') or case.get('weight_format') or '--'} "
            f"| {case.get('pipeline_mode') or '--'} | {case.get('kv_cache_precision') or '--'} "
            f"| {_mtp_cell(case, empty='off').strip() or 'off'} | {case.get('status')} "
            f"| {case.get('iterations_measured', 0)} | {_output_tokens_cell(case)} | "
            + " | ".join(timings)
            + f" | {_fmt(case.get('peak_ram_gb'))}/{_fmt(case.get('mean_ram_gb'))} "
            f"| {_fmt(case.get('peak_gpu_gb'))}/{_fmt(case.get('mean_gpu_gb'))} "
            f"({_fmt(case.get('peak_gpu_pct_of_budget'))}%/{_fmt(case.get('mean_gpu_pct_of_budget'))}%) "
            f"| {_fmt(case.get('expected_kv_gb'), '{:.2f}')} | {cache} |"
        )
    lines.append("")
    if any(case.get("mtp") for case in at_context):
        lines += [_ACCEPTANCE_LENGTH_LEGEND, ""]
    if any(case.get("small_sample") for case in at_context):
        lines += [f"Fewer than {metrics.SMALL_SAMPLE_THRESHOLD} measured samples per case: "
                  "p95 is nearest-rank on a small sample, not a stable distribution statistic.", ""]
    if settings.get("accuracy"):
        lines += [
            "Timing medians pool accuracy probes with different prompts and answer lengths; "
            "use a throughput run for repeated-prompt speed comparisons.",
            "",
        ]
        return lines
    if ranked:
        best = ranked[0]
        lines += [
            f"Best TPOT  : {best['profile']} ({best.get('pipeline_mode') or '--'}) -- TPOT "
            f"{_fmt(best.get('other_tokens_avg_latency'))} ms/token "
            f"({_fmt(best.get('decode_throughput'), '{:.2f}')} decode tok/s), TTFT "
            f"{_ttft_seconds(best)}s, peak GPU {_fmt(best.get('peak_gpu_gb'))} GiB",
            f"             ov {best.get('ov_config')}",
            f"             scheduler {best.get('scheduler_config')}",
        ]
    by_ttft = _ttft_leaderboard(at_context)
    if by_ttft:
        fastest = by_ttft[0]
        lines.append(
            f"Fastest TTFT: {fastest['profile']} ({fastest.get('pipeline_mode') or '--'}) -- "
            f"{_ttft_seconds(fastest)}s, prefill {_fmt(fastest.get('prefill_throughput'))} tok/s"
        )
    for note in (_mtp_speedup(at_context), _mtp_output_check(at_context)):
        if note:
            lines.append(note)
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Accuracy sections
# ---------------------------------------------------------------------------
def _suite_summary(case: dict, suite: str) -> dict:
    return (case.get("accuracy") or {}).get(suite) or {}


def _accuracy_leaderboard(cases: list, suite: str = accuracy.SUITE_RETRIEVAL) -> list:
    """Complete `ok` cases best first; partial coverage is reported but not ranked."""
    keys = (("similarity", "rouge_l", "chrf") if suite == accuracy.SUITE_GENERATION
            else ("recall_rate", "exact_match_rate", "token_f1"))
    ranked = [c for c in cases if c.get("status") == "ok"
              and any(_suite_summary(c, suite).get(k) is not None for k in keys)]
    return sorted(ranked, key=lambda c: tuple(-(_suite_summary(c, suite).get(k) or 0.0)
                                              for k in keys))


def _ordered_cases(cases: list, suite: str) -> list:
    ranked = _accuracy_leaderboard(cases, suite)
    ranked_ids = {id(c) for c in ranked}
    return ranked + [c for c in cases if id(c) not in ranked_ids and _suite_summary(c, suite)]


def _accuracy_section(cases: list, context: int, settings: dict,
                      similarity_notes=()) -> list:
    """Every accuracy table for one context, one section per suite that ran."""
    lines = []
    for suite in settings["accuracy"]["suites"]:
        if not any(_suite_summary(c, suite) for c in cases):
            continue
        lines += (_generation_section(cases, context, settings, similarity_notes)
                  if suite == accuracy.SUITE_GENERATION
                  else _retrieval_section(cases, context, settings))
    return lines


def _retrieval_section(cases: list, context: int, settings: dict) -> list:
    """RULER accuracy for one context: a per-task table, then the NIAH depth matrix."""
    section = settings["accuracy"][accuracy.SUITE_RETRIEVAL]
    baseline_name = settings["accuracy"].get("baseline_profile")
    ordered = _ordered_cases(cases, accuracy.SUITE_RETRIEVAL)
    task_names = list(section["tasks"])
    lines = _heading(f"{context_label(context)} -- RETRIEVAL ACCURACY (RULER)")
    lines += [
        "Cells are each task's own RULER score: share of planted items found, or "
        "intersection-over-union for the aggregation tasks (cwe/fwe) which must punish "
        "over-answering. Decoys is the share of planted decoys that wrongly appeared -- lower "
        "is better, and it is the precision signal recall cannot give."
        + (f" Delta is the overall recall gap to `{baseline_name}`." if baseline_name else ""),
        "",
        f"| Profile | Pipeline | Speculative | {' | '.join(task_names)} | Overall | EM | F1 | "
        f"Decoys | Delta vs {baseline_name or '--'} |",
        "|---|---|---|" + "|".join(["---"] * len(task_names)) + "|---|---|---|---|---|",
    ]
    for case in ordered:
        summary = _suite_summary(case, accuracy.SUITE_RETRIEVAL)
        per_task = summary.get("per_task", {})
        cells = [_pct((per_task.get(task) or {}).get("recall_rate")) for task in task_names]
        lines.append(
            f"| {case['profile']} | {case.get('pipeline_mode') or '--'} "
            f"| {_mtp_cell(case, empty='off').strip() or 'off'} | " + " | ".join(cells)
            + f" | {_pct(summary.get('recall_rate'))} | {_pct(summary.get('exact_match_rate'))} "
            f"| {_pct(summary.get('token_f1'))} | {_pct(summary.get('distractor_rate'))} "
            f"| {_pp((summary.get('deltas') or {}).get('recall_rate_delta'))} |"
        )
    lines.append("")
    depths = list(section["depths"])
    swept = [t for t in task_names if tasks.is_depth_swept(t)]
    if swept and depths:
        lines += [
            f"Depth sweep -- recall by where the fact was planted ({', '.join(swept)}); 0.00 is "
            "the top of the transcript, furthest from the question.",
            "",
            f"| Profile | {' | '.join(f'd={d:.2f}' for d in depths)} |",
            "|---|" + "|".join(["---"] * len(depths)) + "|",
        ]
        for case in ordered:
            per_depth = _suite_summary(case, accuracy.SUITE_RETRIEVAL).get("per_depth", {})
            cells = [_pct((per_depth.get(f"{d:.2f}") or {}).get("recall_rate")) for d in depths]
            lines.append(f"| {case['profile']} | " + " | ".join(cells) + " |")
        lines.append("")
    ranked = _accuracy_leaderboard(cases, accuracy.SUITE_RETRIEVAL)
    if ranked:
        best = _suite_summary(ranked[0], accuracy.SUITE_RETRIEVAL)
        lines += [
            f"Best retrieval: {ranked[0]['profile']} -- recall {_pct(best.get('recall_rate'))}, "
            f"EM {_pct(best.get('exact_match_rate'))}, F1 {_pct(best.get('token_f1'))} over "
            f"{best.get('probe_count')} probes",
            "",
        ]
    return lines


def _generation_section(cases: list, context: int, settings: dict,
                        similarity_notes=()) -> list:
    """Fidelity for one context: each profile's real-task output against the baseline
    profile's output on the identical prompt."""
    baseline_name = settings["accuracy"].get("baseline_profile")
    ordered = _ordered_cases(cases, accuracy.SUITE_GENERATION)
    units = sorted({p.get("divergence_unit") for c in ordered for p in c.get("probes", [])
                    if p.get("divergence_unit")})
    unit = units[0] if len(units) == 1 else ("mixed units" if units else "tokens")
    lines = _heading(f"{context_label(context)} -- GENERATION FIDELITY (who_what_benchmark)")
    lines += [
        "The model does the real classroom task at full answer length; each answer is scored "
        f"against the baseline profile `{baseline_name or '--'}`'s answer to the identical "
        "prompt. This is the measurement retrieval cannot make -- a profile can return every "
        "planted code and still write worse prose.",
        "",
        f"| Profile | Pipeline | Speculative | Similarity | ROUGE-1 | ROUGE-2 | ROUGE-L | chrF "
        f"| FDT ({unit}) | SDT norm | Identical | Coverage |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for case in ordered:
        summary = _suite_summary(case, accuracy.SUITE_GENERATION)
        lines.append(
            f"| {case['profile']} | {case.get('pipeline_mode') or '--'} "
            f"| {_mtp_cell(case, empty='off').strip() or 'off'} "
            f"| {_num(summary.get('similarity'), '{:.4f}')} "
            f"| {_num(summary.get('rouge1'))} | {_num(summary.get('rouge2'))} "
            f"| {_num(summary.get('rouge_l'))} | {_num(summary.get('chrf'))} "
            f"| {_num(summary.get('fdt'), '{:.1f}')} | {_num(summary.get('sdt_norm'))} "
            f"| {_pct(summary.get('identical_rate'))} | {_pct(summary.get('fact_coverage'))} |"
        )
    lines += [
        "",
        "Similarity is an embedding cosine (semantic); ROUGE/chrF are lexical overlap; FDT is "
        f"how many {unit} the two answers agreed on before first differing and SDT norm the "
        "share that differs (lower better); Identical is a byte-for-byte match. Each family "
        "is blind to something the next catches -- a reworded but equivalent answer scores "
        "~1.0 on similarity and well below it on ROUGE. Coverage is the only column scored "
        "against real ground truth rather than another profile's output.",
        f"The `{baseline_name or '--'}` row compares against itself, so it reads 1.00 "
        "throughout by construction: a sanity check, not a result.",
        "",
    ]
    for note in sorted(similarity_notes or ()):
        lines += [f"NOTE: similarity not measured -- {note}", ""]

    ranked = _accuracy_leaderboard(cases, accuracy.SUITE_GENERATION)
    # Only profiles that drifted: a byte-identical profile is not the "largest drift".
    drifted = [
        c for c in ranked
        if c["profile"] != baseline_name
        and (_suite_summary(c, accuracy.SUITE_GENERATION).get("identical_rate") or 0.0) < 1.0
    ]
    if not drifted:
        others = [c for c in ranked if c["profile"] != baseline_name]
        if others:
            lines += [
                f"No drift: every one of {len(others)} profile(s) reproduced the baseline's "
                "answers byte for byte.",
                "",
            ]
    else:
        worst = _suite_summary(drifted[-1], accuracy.SUITE_GENERATION)
        # Dropped rather than printed as "--", which would read as a low score.
        semantic = (f"similarity {_num(worst.get('similarity'), '{:.4f}')}, "
                    if worst.get("similarity") is not None else "")
        lines += [
            f"Largest drift: {drifted[-1]['profile']} -- {semantic}ROUGE-L "
            f"{_num(worst.get('rouge_l'))}, chrF {_num(worst.get('chrf'))}, first "
            f"divergence after {_num(worst.get('fdt'), '{:.1f}')} {unit} over "
            f"{worst.get('probe_count')} probes",
            "",
        ]
    return lines


# ---------------------------------------------------------------------------
# Detail, failures, header
# ---------------------------------------------------------------------------
def _probe_detail(cases: list, record_text: bool) -> list:
    rows = [(c, p) for c in cases for p in c.get("probes", [])]
    if not rows:
        return []
    lines = _heading(f"PROBE DETAIL ({len(rows)} probe(s))")
    for case, probe in rows:
        depth = metrics.finite_number(probe.get("depth"))
        where = f"d={depth:.2f}" if depth is not None else "  -  "
        coordinates = (f"{case['profile']:<12} {str(probe.get('task', '?')):<16} {where} "
                       f"#{probe.get('sample')}")
        if probe.get("status") == "skipped":
            lines.append(f"{coordinates}  SKIPPED: {_one_line(probe.get('skip_reason'), 140)}")
            continue
        if probe.get("suite") == accuracy.SUITE_GENERATION:
            scores = (f"rougeL {_num(probe.get('rouge_l'))} chrf {_num(probe.get('chrf'))} "
                      f"fdt {_num(probe.get('fdt'), '{:g}')} sim {_num(probe.get('similarity'), '{:.4f}')} "
                      f"cover {_num(probe.get('fact_coverage'))}")
            if probe.get("fidelity_note"):
                scores += f" ({probe['fidelity_note']})"
        else:
            scores = (f"recall {_num(probe.get('recall'))} em {1 if probe.get('exact_match') else 0} "
                      f"f1 {_num(probe.get('token_f1'))} decoy {_num(probe.get('distractor_rate'))}")
        input_size = probe.get("input_size")
        size = f"{input_size:,} tok" if isinstance(input_size, int) else "-- tok"
        trunc = " [ceiling reached: potentially truncated]" if probe.get("potentially_truncated") else ""
        lines.append(f"{coordinates}  {size}  {scores}{trunc}")
        if record_text:
            lines += [
                f"    truth  : {_one_line(probe.get('truth'), 110)}",
                f"    answer : {_one_line(probe.get('prediction'), 110)}",
            ]
    if not record_text:
        lines.append("(answer and truth text not recorded; set benchmark.record_text: true for "
                     "synthetic answer persistence)")
    lines.append("")
    return lines


def _failure_section(snapshot: dict, cases: list) -> list:
    failures = [c for c in cases if c.get("status") != "ok" or c.get("flags")]
    warnings = list(snapshot.get("warnings") or [])
    not_run = [p for p in snapshot.get("plan") or [] if p.get("state") != "done"]
    if not (failures or warnings or not_run):
        return []
    lines = _heading("FAILURES, PARTIAL COVERAGE AND WARNINGS")
    for case in failures:
        flags = f" [flags: {', '.join(case['flags'])}]" if case.get("flags") else ""
        lines.append(
            f"{case.get('case_id') or '--'} {case.get('model')} / {case.get('profile')} "
            f"@ {context_label(case.get('context_tokens'))}"
            f"{' / ' + case['throughput_task'] if case.get('throughput_task') else ''} -- "
            f"{case.get('status')}{flags}: {_one_line(_error_text(case) or '; '.join(case.get('measurement_errors') or []), 240)}"
        )
        for warning in case.get("warnings") or []:
            lines.append(f"    warning: {_one_line(warning, 200)}")
    for entry in not_run:
        lines.append(f"{entry.get('case_id')} not run: {entry.get('label')} ({entry.get('state')})")
    for warning in warnings:
        lines.append(f"warning: {_one_line(warning, 240)}")
    lines.append("")
    return lines


def _header(snapshot: dict) -> list:
    settings = snapshot.get("settings") or {}
    env = snapshot.get("environment") or {}
    counts = snapshot.get("counts") or {}
    state = ("complete" if snapshot.get("completed") else
             "INTERRUPTED" if snapshot.get("interrupted") else "IN PROGRESS / ENDED EARLY")
    runtime = env.get("packages") or {}
    gap = settings.get("iteration_gap_sec") or 0
    acc = settings.get("accuracy")
    workload = (
        "accuracy probes (per-suite decode ceilings; EOS respected)" if acc else
        f"{settings.get('iterations')} measured iteration(s) @ up to {settings.get('output_tokens')} "
        "output tokens (EOS respected)"
    ) + (f", {gap:g}s idle before each measured run (burst)" if gap else ", back-to-back (sustained)")
    budget = settings.get("gpu_memory_budget_gb")
    lines = [
        "=" * 78,
        f" Context Bench -- {', '.join(settings.get('models') or [])}",
        "=" * 78,
        f"Run       : {snapshot.get('run_id')} revision {snapshot.get('revision')} [{state}]",
        f"Time (UTC): started {snapshot.get('started_utc')}, ended {snapshot.get('ended_utc') or '--'}",
        f"Coverage  : {counts.get('planned', 0)} planned, {counts.get('processed', 0)} processed, "
        f"{counts.get('ok', 0)} ok, {counts.get('failed', 0)} not ok, "
        f"{counts.get('not_run', 0)} not run, {counts.get('skipped_matrix_points', 0)} "
        "case(s) skipped by the matrix",
        f"Tool      : commit {(snapshot.get('tool') or {}).get('commit') or 'unknown'} | config "
        f"{(snapshot.get('source_config') or {}).get('path')} sha256 "
        f"{((snapshot.get('source_config') or {}).get('sha256') or 'unknown')[:16]}",
        f"Hardware  : {env.get('processor') or 'unknown'}, {env.get('memory_gb') or 'unknown'} GiB RAM, "
        f"device {', '.join(env.get('device_names') or []) or 'unknown'}",
        "Runtime   : " + ", ".join(f"{k} {v or 'unknown'}" for k, v in runtime.items()),
        f"Device    : {settings.get('device')} | weights label {settings.get('weight_format')} | "
        f"budgets host RAM <= {settings.get('max_system_memory_pct')}%"
        + (f", GPU <= {budget:g} GiB" if budget is not None and str(settings.get('device', '')).upper().startswith("GPU")
           else " (no GPU budget)"),
        f"Contexts  : {', '.join(context_label(c) for c in settings.get('context_tokens') or [])} | "
        f"{settings.get('warmup')} warmup + {workload}",
    ]
    variants = {p.get("throughput_task"): p["task_variant"]
                for p in snapshot.get("plan") or [] if p.get("task_variant")}
    if variants:
        lines.append("Tasks     : " + ", ".join(f"{task} resolved as {variant} (app config.yaml)"
                                                for task, variant in variants.items()))
    if acc:
        lines.append(f"Accuracy  : {', '.join(acc['suites'])} | baseline "
                     f"{acc.get('baseline_profile') or '(none)'} | seed {acc['seed']}")
    if settings.get("smoke_test"):
        lines.append("NOTE      : smoke test (zero warm-up or one measured iteration)")
    lines += ["", _MEMORY_LEGEND, ""]
    return lines


def _plan_section(snapshot: dict) -> list:
    """The planned cases by ID, so every table row and console banner maps to its step."""
    plan = snapshot.get("plan") or []
    if not plan:
        return []
    lines = _heading(f"CASES ({len(plan)} planned, run row by row)")
    lines += plan_settings.plan_grid(plan)
    pending = [p for p in plan if p.get("state") != "done"]
    if pending:
        lines.append("")
        lines += [f"  {p['case_id']}: {p.get('state')}" for p in pending]
    lines.append("")
    return lines


def render_report(snapshot: dict) -> list:
    """The whole report as lines, from a results snapshot."""
    settings = snapshot.get("settings") or {}
    cases = list(snapshot.get("cases") or [])
    lines = _header(snapshot) + _plan_section(snapshot)
    for context in settings.get("context_tokens") or []:
        at_context = [c for c in cases if c.get("context_tokens") == context]
        if not at_context:
            continue
        groups = _task_groups(at_context)
        for task, at_task in groups:
            lines += _speed_section(at_task, context, settings, task=task if len(groups) > 1 else None)
        lines += _window_sweep_section(at_context, context, settings.get("speculative_reference"))
        if settings.get("accuracy"):
            lines += _accuracy_section(at_context, context, settings,
                                       snapshot.get("similarity_notes") or ())
    lines += _probe_detail(cases, bool(settings.get("record_text")))
    lines += _failure_section(snapshot, cases)
    return lines


def format_case_line(case: dict) -> str:
    """The per-case console line."""
    if case.get("status") not in _USABLE_STATUSES:
        return f"  => {str(case.get('status')).upper()} ({_error_text(case)})"
    if case.get("accuracy"):
        parts = []
        retrieval = case["accuracy"].get(accuracy.SUITE_RETRIEVAL)
        if retrieval:
            parts.append(f"retrieval recall {_pct(retrieval.get('recall_rate'))} "
                         f"(EM {_pct(retrieval.get('exact_match_rate'))}, "
                         f"F1 {_pct(retrieval.get('token_f1'))}) over "
                         f"{retrieval.get('probe_count')} probes")
        generation = case["accuracy"].get(accuracy.SUITE_GENERATION)
        if generation:
            parts.append(f"generation ROUGE-L {_pct(generation.get('rouge_l'))}, coverage "
                         f"{_pct(generation.get('fact_coverage'))} over "
                         f"{generation.get('probe_count')} probes")
        return (f"  => {case['status'].upper()} | " + " | ".join(parts or ["no probes scored"])
                + f" | TPOT {_fmt(case.get('other_tokens_avg_latency'))} ms/token | TTFT {_ttft_seconds(case)}s")
    line = (
        f"  => {case['status'].upper()} | TPOT {_fmt(case.get('other_tokens_avg_latency'))} ms/token "
        f"{_mtp_cell(case, prefix='| Speculative ')}| TTFT {_ttft_seconds(case)}s "
        f"| decode {_fmt(case.get('decode_throughput'), '{:.2f}')} tok/s "
        f"| e2e {_fmt(case.get('e2e_throughput'))} tok/s "
        f"| RAM peak {_fmt(case.get('peak_ram_gb'))} GiB ({_fmt(case.get('peak_ram_pct'))}%) "
        f"| GPU peak {_fmt(case.get('peak_gpu_gb'))} GiB ({_fmt(case.get('peak_gpu_pct_of_budget'))}% of budget)"
    )
    hint = _mtp_tuning_hint(case)
    return line + (f"\n{hint}" if hint else "")

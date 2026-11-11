# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Per-iteration benchmark records and their aggregation, in llm_bench's units.

Field names and units follow openvino.genai/tools/llm_bench so a run here can be read next
to an llm_bench report without a translation table: latencies are milliseconds, generation
and load durations seconds, throughputs tokens/second, memory GiB. Warm-up iterations are
excluded from every aggregate.

Two rules separate this module from a plain port of the reference:

* **Unavailable is null, never zero.** A metric the runtime did not report, or that cannot
  be formed from the values that are present, is ``None`` and stays out of every
  denominator. "Not measured" and "measured as zero" are different findings.
* **Clock boundaries are never mixed.** Each timed field records its source. The decode
  latency fallback ``(1000 * generation_time - TTFT) / (output_size - 1)`` is only formed
  from a matched pair -- both runtime ``perf_metrics`` values or both wall-clock values --
  so a wall-clock TTFT is never subtracted from a runtime duration.

`tokens_per_step` is the speculative-decoding *acceptance length* (AL): tokens committed per
target verification pass, excluding the prefill pass. It is what decode speedup divides by;
the accepted rate ``(AL - 1) / k`` falls with k by construction and cannot rank windows.

Values are stored unrounded; reports round for display.
"""

from __future__ import annotations

import math
import statistics

SOURCE_RUNTIME = "runtime"
SOURCE_WALL = "wall"
SOURCE_DERIVED_RUNTIME = "derived:runtime"
SOURCE_DERIVED_WALL = "derived:wall"
SOURCE_PROMPT = "prompt"

# Below this many measured samples a distribution statistic carries a warning. A reporting
# gate, not a guarantee of statistical stability.
SMALL_SAMPLE_THRESHOLD = 20
PERCENTILE_METHOD = "nearest-rank"

# Written per iteration, in this order. Names/units mirror llm_bench's report columns;
# prefill/decode/e2e throughput are this tool's long-context additions.
ITERATION_FIELDS = [
    "iteration",
    "warmup",
    "input_size",
    "output_size",
    "generation_time",
    "latency",
    "first_token_latency",
    "other_tokens_avg_latency",
    "tokenization_time",
    "detokenization_time",
    "prefill_throughput",
    "decode_throughput",
    "e2e_throughput",
    # Speculative decoding. Present on every record so the evidence keeps one shape; None
    # on a profile that does not speculate.
    "num_assistant_tokens",
    "mtp_draft_tokens",
    "mtp_accepted_tokens",
    "mtp_rejected_tokens",
    "verification_steps",
    "tokens_per_step",
    "mtp_acceptance_rate",
    "mtp_acceptance_estimated",
    # Draft-model inference time as a fraction of the main model's. May include target
    # prefill; it is not a per-decode-pass cost.
    "mtp_draft_to_main_ratio",
    # Per-pass decomposition from the runtime's own inference durations (see `pass_profile`).
    "first_decode_ms",
    "steady_pass_ms",
    "steady_tpot",
    "output_sha256",
    # Where each timed field came from: runtime perf_metrics, wall clock, or derived from a
    # matched pair of either.
    "sources",
]

# Aggregated with median (p50), mean, nearest-rank p95, min and max.
AGGREGATED_METRICS = [
    "generation_time",
    "latency",
    "first_token_latency",
    "other_tokens_avg_latency",
    "prefill_throughput",
    "decode_throughput",
    "e2e_throughput",
    "mtp_draft_tokens",
    "mtp_accepted_tokens",
    "mtp_rejected_tokens",
    "tokens_per_step",
    "mtp_acceptance_rate",
    "mtp_draft_to_main_ratio",
    "first_decode_ms",
    "steady_pass_ms",
    "steady_tpot",
]

# The parent's per-iteration memory windows: median only, because the case-level `peak_*`
# already is the whole-case high-water mark.
MEDIAN_ONLY_METRICS = [
    "mean_ram_gb",
    "mean_gpu_gb",
]


# ---------------------------------------------------------------------------
# Value validity
# ---------------------------------------------------------------------------
def finite_number(value) -> float | None:
    """A finite real number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def positive_number(value) -> float | None:
    value = finite_number(value)
    return value if value is not None and value > 0 else None


def non_negative_number(value) -> float | None:
    value = finite_number(value)
    return value if value is not None and value >= 0 else None


def count_value(value) -> int | None:
    """A non-negative integer count, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _rate(numerator, seconds) -> float | None:
    numerator, seconds = positive_number(numerator), positive_number(seconds)
    if numerator is None or seconds is None:
        return None
    return numerator / seconds


# ---------------------------------------------------------------------------
# Timing selection
# ---------------------------------------------------------------------------
def derived_tpot(generation_time_s, first_token_latency_ms, output_size) -> float | None:
    """Decode latency per token after the first, from one matched pair of measurements.

    Only meaningful for a positive decode duration and more than one output token; the
    first token is produced by prefill and is already accounted for by TTFT.
    """
    generation = positive_number(generation_time_s)
    ttft = positive_number(first_token_latency_ms)
    size = count_value(output_size)
    if generation is None or ttft is None or size is None or size <= 1:
        return None
    decode_ms = generation * 1000.0 - ttft
    return decode_ms / (size - 1) if decode_ms > 0 else None


def select_timing(runtime: dict, wall_generation_time, wall_first_token_latency,
                  output_size) -> tuple:
    """Pick each timed field's value and source; returns ``(values, sources)``.

    Runtime `perf_metrics` win wherever they are valid; wall-clock values are the per-field
    fallback. A missing runtime TPOT is derived from the runtime pair when both of its halves
    exist, otherwise from the wall pair -- never from one of each.
    """
    runtime = runtime or {}
    values, sources = {}, {}

    rt_generation = positive_number(runtime.get("generation_time"))
    rt_ttft = positive_number(runtime.get("first_token_latency"))
    wall_generation = positive_number(wall_generation_time)
    wall_ttft = positive_number(wall_first_token_latency)

    for field, rt_value, wall_value in (
        ("generation_time", rt_generation, wall_generation),
        ("first_token_latency", rt_ttft, wall_ttft),
    ):
        if rt_value is not None:
            values[field], sources[field] = rt_value, SOURCE_RUNTIME
        elif wall_value is not None:
            values[field], sources[field] = wall_value, SOURCE_WALL
        else:
            values[field], sources[field] = None, None

    tpot = positive_number(runtime.get("other_tokens_avg_latency"))
    tpot_source = SOURCE_RUNTIME if tpot is not None else None
    if tpot is None and rt_generation is not None and rt_ttft is not None:
        tpot = derived_tpot(rt_generation, rt_ttft, output_size)
        tpot_source = SOURCE_DERIVED_RUNTIME if tpot is not None else None
    if tpot is None and wall_generation is not None and wall_ttft is not None:
        tpot = derived_tpot(wall_generation, wall_ttft, output_size)
        tpot_source = SOURCE_DERIVED_WALL if tpot is not None else None
    values["other_tokens_avg_latency"], sources["other_tokens_avg_latency"] = tpot, tpot_source

    for field in ("tokenization_time", "detokenization_time"):
        value = non_negative_number(runtime.get(field))
        values[field] = value
        sources[field] = SOURCE_RUNTIME if value is not None else None
    return values, sources


# ---------------------------------------------------------------------------
# Speculative-decoding derivations
# ---------------------------------------------------------------------------
def pass_profile(infer_ms: list | None, batch_sizes: list | None) -> tuple:
    """Split decode into its first pass and its steady state: (first_ms, steady_ms, steady_tpot).

    `infer_ms` is one runtime inference duration per step in ms (the prefill first),
    `batch_sizes` the tokens each step committed. The first decode pass carries one-time
    per-request work and is reported on its own; `steady_pass_ms` is the median of the
    remaining passes and `steady_tpot` their total duration per committed token.

    All None unless the arrays match and there are at least two passes after the first
    decode pass.
    """
    if not infer_ms or not batch_sizes or len(infer_ms) != len(batch_sizes) or len(infer_ms) < 4:
        return None, None, None
    durations = [finite_number(value) for value in infer_ms]
    committed = [count_value(value) for value in batch_sizes]
    if any(value is None or value < 0 for value in durations) or any(
        value is None for value in committed
    ):
        return None, None, None
    steady, steady_tokens = durations[2:], sum(committed[2:])
    if steady_tokens <= 0:
        return None, None, None
    return durations[1], statistics.median(steady), sum(steady) / steady_tokens


def mtp_yield(output_size, verification_steps, num_assistant_tokens) -> tuple:
    """Acceptance length over the decode passes, and the step-derived acceptance estimate.

    ``AL = (output_size - 1) / (verification_steps - 1)``: the first recorded step is the
    prefill pass, which emits one token and verified nothing, so counting it understates
    the yield (45 tokens over 14 passes is AL 44/13, not 45/14). A plain profile yields
    exactly 1.00, a self-check that no draft is engaged.

    The estimate ``(AL - 1) / k`` subtracts the target's own bonus token, which was never
    drafted. Both values are None when either count is missing or not above one.
    """
    size = count_value(output_size)
    steps = count_value(verification_steps)
    if size is None or steps is None or size <= 1 or steps <= 1:
        return None, None
    tokens_per_step = (size - 1) / (steps - 1)
    k = count_value(num_assistant_tokens)
    if not k:
        return tokens_per_step, None
    # Clamped: a runtime whose step series is off by one must not produce a rate outside
    # [0, 1].
    rate = (tokens_per_step - 1.0) / k
    return tokens_per_step, min(1.0, max(0.0, rate))


def _measured_acceptance(rate, drafted, accepted) -> float | None:
    """The runtime's acceptance rate if valid, else accepted/drafted from consistent counts."""
    rate = finite_number(rate)
    if rate is not None and 0.0 <= rate <= 1.0:
        return rate
    drafted, accepted = count_value(drafted), count_value(accepted)
    if drafted and accepted is not None and accepted <= drafted:
        return accepted / drafted
    return None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def iteration_record(
    iteration: int,
    input_size,
    output_size,
    generation_time,
    first_token_latency,
    other_tokens_avg_latency=None,
    tokenization_time=None,
    detokenization_time=None,
    warmup: bool = False,
    num_assistant_tokens=None,
    verification_steps=None,
    mtp_acceptance_rate=None,
    mtp_draft_tokens=None,
    mtp_accepted_tokens=None,
    mtp_rejected_tokens=None,
    mtp_draft_to_main_ratio=None,
    output_sha256=None,
    first_decode_ms=None,
    steady_pass_ms=None,
    steady_tpot=None,
    sources: dict | None = None,
) -> dict:
    """One generation, in llm_bench units (ms for latency, s for time).

    `sources` names where each timed field came from. When TPOT is not supplied it is
    derived here only if generation time and TTFT share a source (or no sources were given,
    meaning the caller vouches for a matched pair).
    """
    sources = dict(sources or {})
    input_size = count_value(input_size)
    output_size = count_value(output_size)
    generation = positive_number(generation_time)
    ttft = positive_number(first_token_latency)
    tpot = positive_number(other_tokens_avg_latency)

    if tpot is None and other_tokens_avg_latency is None:
        generation_source = sources.get("generation_time")
        if generation_source == sources.get("first_token_latency"):
            tpot = derived_tpot(generation, ttft, output_size)
            if tpot is not None:
                sources["other_tokens_avg_latency"] = (
                    f"derived:{generation_source}" if generation_source else "derived"
                )

    tokens_per_step, step_estimate = mtp_yield(output_size, verification_steps,
                                               num_assistant_tokens)
    drafted = count_value(mtp_draft_tokens)
    accepted = count_value(mtp_accepted_tokens)
    measured_rate = _measured_acceptance(mtp_acceptance_rate, drafted, accepted)
    acceptance = measured_rate if measured_rate is not None else step_estimate
    rejected = count_value(mtp_rejected_tokens)
    if drafted is not None and accepted is not None and accepted <= drafted:
        rejected = drafted - accepted
    elif rejected is not None and drafted is not None and rejected > drafted:
        rejected = None

    return {
        "iteration": iteration,
        "warmup": bool(warmup),
        "input_size": input_size,
        "output_size": output_size,
        "generation_time": generation,
        "latency": (generation * 1000.0 / output_size) if generation and output_size else None,
        "first_token_latency": ttft,
        "other_tokens_avg_latency": tpot,
        "tokenization_time": non_negative_number(tokenization_time),
        "detokenization_time": non_negative_number(detokenization_time),
        "prefill_throughput": (1000.0 * input_size / ttft) if ttft and input_size else None,
        "decode_throughput": (1000.0 / tpot) if tpot else None,
        "e2e_throughput": (_rate(input_size + output_size, generation)
                           if input_size is not None and output_size is not None else None),
        "num_assistant_tokens": count_value(num_assistant_tokens),
        "mtp_draft_tokens": drafted,
        "mtp_accepted_tokens": accepted if (drafted is None or accepted is None
                                            or accepted <= drafted) else None,
        "mtp_rejected_tokens": rejected,
        "verification_steps": count_value(verification_steps),
        "tokens_per_step": tokens_per_step,
        "mtp_acceptance_rate": acceptance,
        "mtp_acceptance_estimated": acceptance is not None and measured_rate is None,
        "mtp_draft_to_main_ratio": non_negative_number(mtp_draft_to_main_ratio),
        "first_decode_ms": non_negative_number(first_decode_ms),
        "steady_pass_ms": positive_number(steady_pass_ms),
        "steady_tpot": positive_number(steady_tpot),
        "output_sha256": output_sha256,
        "sources": sources,
    }


def measured(records: list) -> list:
    """The records that count: not warm-up, and not a skipped/failed probe."""
    return [
        r for r in records or []
        if not r.get("warmup") and r.get("status", "ok") == "ok"
    ]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def nearest_rank(values: list, percentile: float) -> float | None:
    """Nearest-rank percentile: the smallest sample with at least `percentile`% at or below."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(values: list) -> dict:
    """Count, total, mean, median (p50), nearest-rank p95, min and max of finite samples."""
    samples = [v for v in (finite_number(value) for value in values) if v is not None]
    if not samples:
        return {"count": 0, "total": None, "mean": None, "median": None, "p50": None,
                "p95": None, "min": None, "max": None,
                "small_sample": True, "percentile_method": PERCENTILE_METHOD}
    median = statistics.median(samples)
    return {
        "count": len(samples),
        "total": sum(samples),
        "mean": sum(samples) / len(samples),
        "median": median,
        "p50": median,
        "p95": nearest_rank(samples, 95),
        "min": min(samples),
        "max": max(samples),
        "small_sample": len(samples) < SMALL_SAMPLE_THRESHOLD,
        "percentile_method": PERCENTILE_METHOD,
    }


def aggregate(records: list) -> dict:
    """Distribution statistics of each metric over the measured iterations.

    The headline value is the median (robust to one slow outlier); `*_min`/`*_max` keep the
    spread visible and `*_mean`/`*_p95`/`*_count` carry the serving statistics contract.
    Nulls never enter a denominator. Aggregate decode throughput is ``1000 / median TPOT``,
    not the mean of per-iteration reciprocals.
    """
    rows = measured(records)
    if not rows:
        return {"iterations_measured": 0}

    out = {
        "iterations_measured": len(rows),
        "small_sample": len(rows) < SMALL_SAMPLE_THRESHOLD,
        "percentile_method": PERCENTILE_METHOD,
        "mtp_acceptance_estimated": any(row.get("mtp_acceptance_estimated") for row in rows),
    }
    for metric in AGGREGATED_METRICS:
        values = [v for v in (finite_number(r.get(metric)) for r in rows) if v is not None]
        if not values:
            continue
        stats = summarize(values)
        out[metric] = stats["median"]
        out[f"{metric}_min"] = stats["min"]
        out[f"{metric}_max"] = stats["max"]
        out[f"{metric}_mean"] = stats["mean"]
        out[f"{metric}_p95"] = stats["p95"]
        out[f"{metric}_count"] = stats["count"]
        # A metric with nulls has fewer samples than iterations; its p95 is small-sample too.
        out["small_sample"] = out["small_sample"] or stats["small_sample"]
        if metric == "generation_time":
            out["generation_time_total"] = stats["total"]
    for metric in MEDIAN_ONLY_METRICS:
        values = [v for v in (finite_number(r.get(metric)) for r in rows) if v is not None]
        if values:
            out[metric] = statistics.median(values)
    for field in ("input_size", "output_size"):
        out[field] = rows[-1].get(field)
    output_sizes = [row["output_size"] for row in rows if count_value(row.get("output_size"))
                    is not None]
    if output_sizes:
        out["output_size_min"] = min(output_sizes)
        out["output_size_max"] = max(output_sizes)
    median_tpot = positive_number(out.get("other_tokens_avg_latency"))
    if median_tpot is not None:
        out["decode_throughput"] = 1000.0 / median_tpot
    else:
        out.pop("decode_throughput", None)
    output_hashes = {row.get("output_sha256") for row in rows if row.get("output_sha256")}
    out["output_consistent"] = len(output_hashes) <= 1 if output_hashes else None
    out["output_sha256"] = next(iter(output_hashes)) if len(output_hashes) == 1 else None
    # The one text all iterations agreed on, kept in memory for locating a fork against the
    # baseline; evidence persistence follows `record_text`.
    out["output_text"] = next(
        (row["output_text"] for row in rows if row.get("output_text") is not None), None
    ) if out["output_sha256"] else None
    return out


# ---------------------------------------------------------------------------
# Console formatting
# ---------------------------------------------------------------------------
def _f(value, spec: str = "{:.1f}") -> str:
    value = finite_number(value)
    return spec.format(value) if value is not None else "--"


def format_iteration(record: dict) -> str:
    """llm_bench-style one-liner, so a live log reads the same as one of its runs."""
    tag = "warmup" if record.get("warmup") else f"iter {record.get('iteration')}"
    input_size = record.get("input_size")
    line = (
        f"[{tag}] input {input_size:,} tok" if isinstance(input_size, int) else f"[{tag}] input -- tok"
    )
    line += (
        f", output {record.get('output_size') if record.get('output_size') is not None else '--'}"
        f" tok, generation {_f(record.get('generation_time'), '{:.2f}')}s"
    )
    if record.get("first_token_latency") is None and record.get("other_tokens_avg_latency") is None:
        return line + format_mtp(record)
    return (
        f"{line}, latency {_f(record.get('latency'))} ms/token | "
        f"1st token {_f(record.get('first_token_latency'))} ms, "
        f"other tokens {_f(record.get('other_tokens_avg_latency'))} ms/token | "
        f"prefill {_f(record.get('prefill_throughput'))} tok/s, "
        f"decode {_f(record.get('decode_throughput'), '{:.2f}')} tok/s, "
        f"e2e {_f(record.get('e2e_throughput'))} tok/s"
    ) + format_mtp(record)


_STRATEGY_LABELS = {"dflash": "dFlash", "eagle3": "Eagle3"}


def strategy_label(record: dict) -> str:
    """Display name of a record's speculative-decoding strategy."""
    strategy = record.get("speculative_strategy")
    return _STRATEGY_LABELS.get(strategy, str(strategy or "speculative"))


def format_mtp(record: dict) -> str:
    """The speculative-decoding tail; empty when the profile ran without it."""
    tokens_per_step = finite_number(record.get("tokens_per_step"))
    if tokens_per_step is None:
        return ""
    k = record.get("num_assistant_tokens")
    text = (
        f" | {strategy_label(record)} k={k} AL {tokens_per_step:.2f} tok/pass" if k
        else f" | AL {tokens_per_step:.2f} tok/pass"
    )
    acceptance = finite_number(record.get("mtp_acceptance_rate"))
    if acceptance is not None:
        accepted = count_value(record.get("mtp_accepted_tokens"))
        drafted = count_value(record.get("mtp_draft_tokens"))
        counts = (
            f", {accepted}/{drafted} candidates"
            if accepted is not None and drafted and accepted <= drafted else ""
        )
        qualifier = " estimated" if record.get("mtp_acceptance_estimated") else ""
        text += f" ({acceptance * 100:.0f}% accepted{qualifier}{counts})"
    ratio = finite_number(record.get("mtp_draft_to_main_ratio"))
    if ratio is not None:
        text += f", draft/main {ratio:.2f}x"
    return text

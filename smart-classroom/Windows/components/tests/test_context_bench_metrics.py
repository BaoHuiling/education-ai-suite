# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""The measurement contract: units, sources, null handling, statistics, ranking and status,
plus strict configuration validation. Hardware-independent."""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from components.context_bench import benchmark, metrics, report, settings, trial_runner


def _record(iteration, generation_time, ttft_ms, warmup=False, output_size=64, input_size=8000,
            tpot=None, **extra):
    return {**metrics.iteration_record(
        iteration=iteration, input_size=input_size, output_size=output_size,
        generation_time=generation_time, first_token_latency=ttft_ms,
        other_tokens_avg_latency=tpot, warmup=warmup, **extra), "status": "ok"}


class TestDeterministicFixture(unittest.TestCase):
    """101 input tokens, 5 output, 0.3 s generation and a matched 100 ms TTFT."""

    def setUp(self):
        self.record = metrics.iteration_record(
            iteration=1, input_size=101, output_size=5, generation_time=0.3,
            first_token_latency=100.0, sources={"generation_time": "wall",
                                                "first_token_latency": "wall"})

    def test_fallback_tpot_and_throughputs(self):
        self.assertAlmostEqual(self.record["other_tokens_avg_latency"], 50.0)
        self.assertAlmostEqual(self.record["decode_throughput"], 20.0)
        self.assertAlmostEqual(self.record["prefill_throughput"], 1010.0)
        self.assertAlmostEqual(self.record["e2e_throughput"], 353.333, places=3)
        self.assertAlmostEqual(self.record["latency"], 60.0)  # includes prefill; not TPOT
        self.assertEqual(self.record["sources"]["other_tokens_avg_latency"], "derived:wall")

    def test_acceptance_length_excludes_the_prefill_pass(self):
        tokens_per_step, _ = metrics.mtp_yield(45, 14, 7)
        self.assertEqual(tokens_per_step, 44 / 13)
        self.assertNotEqual(tokens_per_step, 45 / 14)
        self.assertEqual(metrics.mtp_yield(64, 64, None)[0], 1.0)  # plain yields exactly 1.00


class TestSourcesAndUnits(unittest.TestCase):
    class _Pair:
        def __init__(self, mean):
            self.mean = mean

    def _result(self, **values):
        pair = self._Pair

        class Perf:
            def __getattr__(self, name):
                key = {"get_ttft": "ttft", "get_tpot": "tpot", "get_generate_duration": "generate",
                       "get_tokenization_duration": "tok", "get_detokenization_duration": "detok"}.get(name)
                if key is None:
                    raise AttributeError(name)
                if key not in values:
                    return lambda: (_ for _ in ()).throw(RuntimeError("not in this build"))
                return lambda: pair(values[key])

        return SimpleNamespace(perf_metrics=Perf())

    def test_runtime_durations_are_converted_from_milliseconds(self):
        read = trial_runner.read_perf_metrics(self._result(ttft=237_000.0, generate=247_000.0))
        self.assertEqual(read["first_token_latency"], 237_000.0)
        self.assertAlmostEqual(read["generation_time"], 247.0)
        self.assertNotIn("other_tokens_avg_latency", read)

    def test_non_finite_runtime_values_are_dropped_not_zeroed(self):
        read = trial_runner.read_perf_metrics(self._result(ttft=float("nan"), tpot=-1.0, generate=float("inf")))
        self.assertEqual(read, {})

    def test_runtime_wins_per_field_and_wall_fills_the_rest(self):
        values, sources = metrics.select_timing({"generation_time": 0.3}, 0.31, 100.0, 5)
        self.assertEqual((values["generation_time"], sources["generation_time"]), (0.3, "runtime"))
        self.assertEqual((values["first_token_latency"], sources["first_token_latency"]), (100.0, "wall"))
        # The runtime pair is incomplete, so TPOT comes from the matched *wall* pair.
        self.assertAlmostEqual(values["other_tokens_avg_latency"], (310.0 - 100.0) / 4)
        self.assertEqual(sources["other_tokens_avg_latency"], "derived:wall")

    def test_a_runtime_duration_is_never_paired_with_a_wall_ttft(self):
        values, sources = metrics.select_timing({"generation_time": 0.3}, None, 100.0, 5)
        self.assertIsNone(values["other_tokens_avg_latency"])
        self.assertIsNone(sources["other_tokens_avg_latency"])
        record = metrics.iteration_record(1, 101, 5, 0.3, 100.0,
                                          sources={"generation_time": "runtime",
                                                   "first_token_latency": "wall"})
        self.assertIsNone(record["other_tokens_avg_latency"])
        self.assertIsNone(record["decode_throughput"])

    def test_the_runtime_pair_is_preferred_for_the_fallback(self):
        values, sources = metrics.select_timing({"generation_time": 0.3, "first_token_latency": 100.0},
                                                0.5, 120.0, 5)
        self.assertAlmostEqual(values["other_tokens_avg_latency"], 50.0)
        self.assertEqual(sources["other_tokens_avg_latency"], "derived:runtime")

    def test_runtime_tpot_wins_over_any_derivation(self):
        values, sources = metrics.select_timing({"other_tokens_avg_latency": 42.0}, 0.3, 100.0, 5)
        self.assertEqual((values["other_tokens_avg_latency"], sources["other_tokens_avg_latency"]),
                         (42.0, "runtime"))

    def test_unavailable_tokenization_is_null(self):
        record = metrics.iteration_record(1, 101, 5, 0.3, 100.0)
        self.assertIsNone(record["tokenization_time"])
        self.assertIsNone(record["detokenization_time"])
        self.assertEqual(set(record), set(metrics.ITERATION_FIELDS))


class TestEosAndNulls(unittest.TestCase):
    def test_zero_one_and_many_output_tokens(self):
        zero = metrics.iteration_record(1, 101, 0, 0.3, 100.0)
        self.assertIsNone(zero["latency"])
        self.assertIsNone(zero["other_tokens_avg_latency"])
        one = metrics.iteration_record(1, 101, 1, 0.3, 100.0)
        self.assertAlmostEqual(one["latency"], 300.0)
        self.assertIsNone(one["other_tokens_avg_latency"])
        self.assertIsNone(one["decode_throughput"])
        many = metrics.iteration_record(1, 101, 11, 1.1, 100.0)
        self.assertAlmostEqual(many["other_tokens_avg_latency"], 100.0)

    def test_inconsistent_wall_timings_yield_null_not_negative(self):
        record = metrics.iteration_record(1, 101, 5, 0.05, 100.0)
        self.assertIsNone(record["other_tokens_avg_latency"])

    def test_missing_ttft_keeps_a_usable_record(self):
        record = metrics.iteration_record(1, 101, 5, 0.3, None)
        self.assertIsNone(record["prefill_throughput"])
        self.assertIsNotNone(record["e2e_throughput"])

    def test_one_token_eos_answer_is_valid_but_unranked(self):
        case = {"status": "ok", "other_tokens_avg_latency": None, "first_token_latency": 100.0}
        self.assertEqual(report._leaderboard([case]), [])
        self.assertEqual(report._ttft_leaderboard([case]), [case])


class TestAggregation(unittest.TestCase):
    def test_warmup_is_excluded_from_every_statistic(self):
        rows = [_record(0, 900.0, 890_000.0, warmup=True)] + [_record(i, 250.0, 240_000.0) for i in (1, 2, 3)]
        aggregate = metrics.aggregate(rows)
        self.assertEqual(aggregate["iterations_measured"], 3)
        self.assertEqual(aggregate["generation_time_max"], 250.0)
        self.assertEqual(aggregate["generation_time_total"], 750.0)
        self.assertEqual(metrics.aggregate([rows[0]]), {"iterations_measured": 0})

    def test_nearest_rank_percentiles(self):
        self.assertEqual(metrics.nearest_rank(list(range(1, 11)), 95), 10)
        self.assertEqual(metrics.nearest_rank(list(range(1, 21)), 95), 19)
        self.assertEqual(metrics.nearest_rank(list(range(1, 101)), 95), 95)
        self.assertEqual(metrics.nearest_rank([5.0], 95), 5.0)
        self.assertIsNone(metrics.nearest_rank([], 95))
        stats = metrics.summarize([3.0, 1.0, None, float("nan"), 2.0])
        self.assertEqual((stats["count"], stats["median"], stats["mean"], stats["total"], stats["p95"]),
                         (3, 2.0, 2.0, 6.0, 3.0))
        self.assertTrue(stats["small_sample"])
        self.assertFalse(metrics.summarize(list(range(20)))["small_sample"])

    def test_median_mean_p95_and_spread_are_reported(self):
        rows = [_record(i, t, 1000.0) for i, t in enumerate((247.0, 250.0, 349.5), 1)]
        aggregate = metrics.aggregate(rows)
        self.assertEqual(aggregate["generation_time"], 250.0)
        self.assertAlmostEqual(aggregate["generation_time_mean"], (247.0 + 250.0 + 349.5) / 3)
        self.assertEqual(aggregate["generation_time_p95"], 349.5)
        self.assertEqual((aggregate["generation_time_min"], aggregate["generation_time_max"]), (247.0, 349.5))
        self.assertTrue(aggregate["small_sample"])

    def test_decode_throughput_is_the_reciprocal_of_median_tpot(self):
        rows = [_record(0, 6.0, 3000.0, output_size=6, tpot=80.0),
                _record(1, 6.0, 3000.0, output_size=87, tpot=20.0)]
        aggregate = metrics.aggregate(rows)
        self.assertEqual(aggregate["other_tokens_avg_latency"], 50.0)
        self.assertEqual(aggregate["decode_throughput"], 20.0)  # not mean(1000/80, 1000/20)
        self.assertEqual(report._output_tokens_cell(aggregate), "6-87")

    def test_nulls_never_enter_a_denominator(self):
        aggregate = metrics.aggregate([_record(1, 247.0, None), _record(2, 250.0, None)])
        self.assertNotIn("first_token_latency", aggregate)
        self.assertNotIn("decode_throughput", aggregate)
        rows = [{"warmup": False, "mean_ram_gb": 40.0}, {"warmup": False, "mean_ram_gb": None},
                {"warmup": False, "mean_ram_gb": 42.0}]
        self.assertEqual(metrics.aggregate(rows)["mean_ram_gb"], 41.0)

    def test_skipped_probes_are_not_measured(self):
        rows = [_record(1, 1.0, 100.0), {"iteration": 2, "warmup": False, "status": "skipped"}]
        self.assertEqual(metrics.aggregate(rows)["iterations_measured"], 1)


class TestSpeculativeMetrics(unittest.TestCase):
    def test_measured_acceptance_wins_and_the_estimate_is_labelled(self):
        measured = metrics.iteration_record(1, 8000, 64, 5.0, 3000.0, num_assistant_tokens=7,
                                            verification_steps=20, mtp_draft_tokens=100,
                                            mtp_accepted_tokens=40)
        self.assertEqual(measured["mtp_acceptance_rate"], 0.4)
        self.assertFalse(measured["mtp_acceptance_estimated"])
        self.assertEqual(measured["mtp_rejected_tokens"], 60)
        estimated = metrics.iteration_record(1, 8000, 64, 5.0, 3000.0, num_assistant_tokens=7,
                                             verification_steps=20)
        self.assertTrue(estimated["mtp_acceptance_estimated"])
        self.assertIn("estimated", metrics.format_iteration(estimated))
        neither = metrics.iteration_record(1, 8000, 64, 5.0, 3000.0, num_assistant_tokens=7)
        self.assertIsNone(neither["mtp_acceptance_rate"])
        self.assertFalse(neither["mtp_acceptance_estimated"])

    def test_inconsistent_counts_are_not_a_ratio(self):
        record = metrics.iteration_record(1, 8000, 64, 5.0, 3000.0, num_assistant_tokens=7,
                                          verification_steps=30, mtp_draft_tokens=0,
                                          mtp_accepted_tokens=34)
        self.assertTrue(record["mtp_acceptance_estimated"])
        self.assertNotIn("34/0", metrics.format_iteration(record))

    def test_steady_pass_requires_two_passes_after_the_first_decode(self):
        first, steady, tpot = metrics.pass_profile([3002.7, 163.3, 95.0, 51.0, 51.1, 55.4],
                                                   [1, 4, 4, 4, 3, 4])
        self.assertEqual(first, 163.3)
        self.assertAlmostEqual(steady, 53.25)
        self.assertAlmostEqual(tpot, (95.0 + 51.0 + 51.1 + 55.4) / 15)
        self.assertEqual(metrics.pass_profile([3000.0, 60.0, 24.0], [1, 1, 1]), (None, None, None))
        self.assertEqual(metrics.pass_profile([3000.0, 60.0, 24.0, 24.0], [1, 1, 1]), (None, None, None))
        self.assertEqual(metrics.pass_profile([1.0, float("nan"), 2.0, 2.0], [1, 1, 1, 1]), (None, None, None))

    def test_dflash_processed_candidates_replace_a_zero_draft_count(self):
        extended = SimpleNamespace(get_draft_acceptance_rate=lambda: float("nan"),
                                   get_num_draft_tokens=lambda: 0, get_num_accepted_tokens=lambda: 31,
                                   get_num_rejected_tokens=lambda: 0,
                                   get_num_draft_processed_tokens=lambda: 91)
        perf = trial_runner.read_perf_metrics(SimpleNamespace(perf_metrics=SimpleNamespace(),
                                                              extended_perf_metrics=extended))
        self.assertNotIn("mtp_acceptance_rate", perf)
        self.assertEqual((perf["mtp_draft_tokens"], perf["mtp_accepted_tokens"], perf["mtp_rejected_tokens"]),
                         (91, 31, 60))


def _case(profile, tpot, ttft=3000.0, mtp=False, key="k", status="ok", **extra):
    return {"model": "m", "profile": profile, "status": status, "mtp": mtp,
            "other_tokens_avg_latency": tpot, "first_token_latency": ttft,
            "comparison_key": key, "cache_pool": "runtime", **extra}


class TestRankingAndMatching(unittest.TestCase):
    def test_tpot_first_then_ttft(self):
        cases = [_case("a", 170.0, 200.0), _case("b", 160.0, 300.0), _case("c", 160.0, 250.0)]
        self.assertEqual([c["profile"] for c in report._leaderboard(cases)], ["c", "b", "a"])

    def test_only_complete_budget_measured_ok_cases_are_ranked(self):
        cases = [_case("usable", 100.0)] + [
            _case(status, 1.0, status=status)
            for status in ("measurement_error", "gpu_memory_limit", "memory_limit", "partial", "oom")
        ]
        self.assertEqual([c["profile"] for c in report._leaderboard(cases)], ["usable"])

    def test_matched_baseline_requires_the_same_comparison_key(self):
        spec = _case("dflash_k7", 20.0, mtp=True, key="same")
        self.assertIsNone(report._matched_baseline(spec, [_case("paged_min", 30.0, key="other")]))
        baseline = _case("paged_min", 30.0, key="same")
        self.assertIs(report._matched_baseline(spec, [baseline]), baseline)
        self.assertIsNone(report._matched_baseline(spec, [_case("other_spec", 10.0, mtp=True, key="same")]))

    def test_comparison_key_covers_prompt_settings_and_plan(self):
        base = {"mode": "throughput", "model": "m", "artifact_id": "a", "weight_precision": "int4_asym",
                "weight_format": "int4", "device": "GPU", "context_tokens": 8000,
                "pipeline_mode": "paged", "throughput_task": "math", "task_sha256": "t",
                "prompt_sha256": "p", "prompt_tokens": 8000, "warmup": 1, "planned_iterations": 3,
                "iteration_gap_sec": 0.0, "output_tokens": 512, "effective_ov": {"A": "1"},
                "effective_scheduler": {"max_num_seqs": 1}, "cache_pool": "runtime"}
        key = benchmark._comparison_key(base)
        for field, value in (("prompt_sha256", "q"), ("artifact_id", "b"), ("warmup", 0),
                             ("effective_ov", {"A": "2"}), ("effective_scheduler", {"max_num_seqs": 2}),
                             ("weight_precision", "int8_asym"), ("iteration_gap_sec", 30.0)):
            with self.subTest(field=field):
                self.assertNotEqual(benchmark._comparison_key({**base, field: value}), key)
        self.assertIsNone(benchmark._comparison_key({**base, "mode": "accuracy"}))

    def test_auto_pools_of_different_size_match_but_are_confounded(self):
        base = {"mode": "throughput", "prompt_sha256": "p", "cache_pool": "auto",
                "effective_scheduler": {"cache_size": 4, "max_num_seqs": 1}}
        other = {**base, "effective_scheduler": {"cache_size": 8, "max_num_seqs": 1}}
        self.assertEqual(benchmark._comparison_key(base), benchmark._comparison_key(other))
        self.assertTrue(report._confounded({"cache_pool": "auto", "cache_size_gb": 4},
                                           {"cache_pool": "auto", "cache_size_gb": 8}))

    def test_speedups_are_reported_separately_and_from_tpot_and_per_token_call_time(self):
        # The speculative answer is shorter (forked), so its generation time alone would
        # overstate the speedup; the total-call figure is per output token.
        cases = [_case("paged_min", 24.0, generation_time=10.0, latency=25.0, steady_tpot=23.5,
                       steady_pass_ms=23.5, first_decode_ms=140.0, throughput_task="math"),
                 _case("dflash_k3", 12.0, mtp=True, speculative_strategy="dflash", num_assistant_tokens=3,
                       tokens_per_step=3.6, generation_time=5.0, latency=20.0, steady_tpot=11.75,
                       steady_pass_ms=52.0, first_decode_ms=210.0, throughput_task="math")]
        line = report._mtp_speedup(cases)
        self.assertIn("decode speedup 2.00x", line)
        self.assertIn("total-call speedup 1.25x (generation time per output token)", line)
        self.assertIn("first decode pass (210 vs 140 ms)", line)
        self.assertIn("2.00x.", line)  # steady 23.5 / 11.75
        rows = report._window_sweep_rows(cases)
        self.assertAlmostEqual(rows[0]["pass_cost"], 52.0 / 23.5)


class TestStatusPrecedence(unittest.TestCase):
    def _case(self, **values):
        base = {"mode": "throughput", "planned_iterations": 3, "completed_iterations": 3,
                "load_ok": True, "measurement_errors": [], "error": None}
        base.update(values)
        return base

    def test_documented_order(self):
        self.assertEqual(benchmark._evaluate_status(self._case()), ("ok", []))
        status, flags = benchmark._evaluate_status(self._case(
            error={"category": "oom", "stage": "decode"}, completed_iterations=1,
            measurement_errors=["x"], gpu_budget_exceeded=True))
        self.assertEqual(status, "oom")
        self.assertEqual(flags, ["partial", "measurement_error", "gpu_memory_limit"])
        self.assertEqual(benchmark._status(self._case(completed_iterations=0)), "no_output")
        self.assertEqual(benchmark._status(self._case(completed_iterations=2,
                                                      system_memory_limit_exceeded=True)), "partial")
        self.assertEqual(benchmark._status(self._case(measurement_errors=["no RAM"],
                                                      gpu_budget_exceeded=True)), "measurement_error")
        self.assertEqual(benchmark._status(self._case(gpu_budget_exceeded=True,
                                                      system_memory_limit_exceeded=True)), "gpu_memory_limit")
        self.assertEqual(benchmark._status(self._case(system_memory_limit_exceeded=True)), "memory_limit")

    def test_failure_categories(self):
        for category, expected in (("timeout", "timeout"), ("crashed", "crashed"),
                                   ("gpu_abort", "gpu_abort"), ("unsupported", "unsupported"),
                                   ("missing_ir", "missing_ir")):
            self.assertEqual(benchmark._status(self._case(error={"category": category})), expected)
        self.assertEqual(benchmark._status(self._case(load_ok=False, completed_iterations=0,
                                                      error={"category": "error", "stage": "load"})),
                         "load_error")
        self.assertEqual(benchmark._status(self._case(error={"category": "prompt", "stage": "prompt"},
                                                      completed_iterations=0)), "error")
        self.assertEqual(benchmark._status(self._case(error={"category": "no_output"},
                                                      completed_iterations=1)), "partial")

    def test_precision_mismatch_and_contamination_are_flags(self):
        _, flags = benchmark._evaluate_status(self._case(precision_label_mismatch=True,
                                                         potentially_contaminated=True))
        self.assertEqual(flags, ["precision_label_mismatch", "potentially_contaminated"])

    def test_errors_are_classified_from_runtime_text(self):
        for text, expected in (
            ("Property KV_CACHE_PRECISION with value u4 is not supported by GPU plugin", "unsupported"),
            ("Requested cache size is larger than available memory size on the system.", "oom"),
            ("Request 0 was dropped by the scheduler because it did not fit in the available cache budget", "oom"),
            ("clWaitForEvents CL_EXEC_STATUS_ERROR_FOR_EVENTS_IN_WAIT_LIST", "gpu_abort"),
            ("no_output: generate() produced no tokens", "no_output"),
            ("something else", "error"),
        ):
            self.assertEqual(trial_runner.classify_error(RuntimeError(text)), expected)
        self.assertEqual(trial_runner.failing_stage(trial_runner.STAGE_PROMPT_BUILT), "prefill")
        self.assertEqual(trial_runner.failing_stage(trial_runner.STAGE_PREFILLED), "decode")


class TestFinalizeMemory(unittest.TestCase):
    SETTINGS = {"device": "CPU", "max_system_memory_pct": 80.0}

    def _case(self, **values):
        case = {"mode": "throughput", "planned_iterations": 1, "load_ok": True, "error": None,
                "measurement_errors": [], "gpu_budget_gb": None, "flags": [],
                "iterations": [{**_record(0, 1.0, 100.0), "peak_ram_gb": 10.0, "memory_window": {}}],
                "prompt": {"prompt_sha256": "p"}, "effective_scheduler": {}, "effective_ov": {}}
        case.update(values)
        return case

    def test_cpu_cases_need_host_ram_only(self):
        case = benchmark._finalize_case(self._case(peak_ram_pct=50.0, peak_gpu_gb=None), self.SETTINGS, [])
        self.assertEqual(case["status"], "ok")
        self.assertFalse(case["gpu_budget_exceeded"])

    def test_missing_host_telemetry_is_a_measurement_error(self):
        case = benchmark._finalize_case(self._case(peak_ram_pct=None), self.SETTINGS, [])
        self.assertEqual(case["status"], "measurement_error")

    def test_gpu_cases_need_gpu_telemetry_and_apply_the_configured_budget(self):
        gpu = {"device": "GPU", "max_system_memory_pct": 80.0}
        missing = benchmark._finalize_case(self._case(peak_ram_pct=50.0, peak_gpu_gb=None, gpu_budget_gb=59.0), gpu, [])
        self.assertIn("GPU memory telemetry unavailable", missing["measurement_errors"])
        over = benchmark._finalize_case(self._case(peak_ram_pct=50.0, peak_gpu_gb=60.0, gpu_budget_gb=59.0,
                                                   gpu_budget_driver_gb=20.0), gpu, [])
        self.assertEqual(over["status"], "gpu_memory_limit")  # the driver total is descriptive only

    def test_runtime_input_count_mismatch_invalidates_certification(self):
        record = {**_record(0, 1.0, 100.0), "peak_ram_gb": 10.0, "memory_window": {},
                  "input_count_mismatch": True}
        case = benchmark._finalize_case(self._case(peak_ram_pct=50.0, iterations=[record]), self.SETTINGS, [])
        self.assertEqual(case["status"], "measurement_error")
        self.assertIsNotNone(case["other_tokens_avg_latency"])  # timings retained


def _raw():
    return {
        "model": {"provider": "openvino", "device": "GPU", "weight_format": "int4",
                  "models_base_path": "models"},
        "benchmark": {"models": ["M"], "context_tokens": [8000], "output_tokens": 64, "warmup": 1,
                      "iterations": 3, "timeout_sec": 600, "gpu_memory_budget_gb": 59,
                      "output_dir": "monitoring/executionlogs/context_bench/test"},
        "profiles": [
            {"name": "paged_min", "ov": {"ATTENTION_BACKEND": "PA"},
             "scheduler": {"max_num_batched_tokens": 8192, "max_num_seqs": 1, "enable_prefix_caching": False}},
            {"name": "dflash_k3", "ov": {"ATTENTION_BACKEND": "PA"},
             "scheduler": {"max_num_batched_tokens": 8192, "max_num_seqs": 1, "enable_prefix_caching": False},
             "dflash": {"model": "models/d", "num_assistant_tokens": 3}},
        ],
    }


class TestSettingsValidation(unittest.TestCase):
    def _load(self, raw, *argv):
        return settings._resolve_settings(raw, settings._parse_args(list(argv)))

    def _bench(self, **values):
        raw = _raw()
        raw["benchmark"].update(values)
        return raw

    def test_precedence_cli_over_yaml_over_default(self):
        resolved = self._load(self._bench(iteration_gap_sec=30))
        self.assertEqual(resolved["iteration_gap_sec"], 30.0)
        self.assertEqual(self._load(self._bench(iteration_gap_sec=30), "--iteration-gap", "0")["iteration_gap_sec"], 0.0)
        default = self._load(_raw())
        self.assertEqual((default["iteration_gap_sec"], default["max_system_memory_pct"],
                          default["throughput_task"], default["record_text"]), (0.0, 80.0, "summary_2s", False))

    def test_the_resolved_settings_are_frozen(self):
        resolved = self._load(_raw())
        with self.assertRaises(TypeError):
            resolved["warmup"] = 0
        with self.assertRaises(TypeError):
            resolved["profiles"][0]["ov"]["X"] = 1

    def test_scalar_type_and_range_rules(self):
        for values, message in (
            ({"iterations": True}, "integer"),
            ({"warmup": -1}, ">= 0"),
            ({"output_tokens": 1}, ">= 2"),
            ({"timeout_sec": 0}, "timeout_sec"),
            ({"timeout_sec": float("inf")}, "finite"),
            ({"gpu_memory_budget_gb": float("nan")}, "finite"),
            ({"gpu_memory_budget_gb": "59"}, "finite"),
            ({"max_system_memory_pct": 120}, "<= 100"),
            ({"iteration_gap_sec": True}, "finite"),
            ({"iteration_gap_sec": 600}, "smaller than timeout_sec"),
            ({"context_tokens": [8000, "bad"]}, "positive integers"),
            ({"context_tokens": [True]}, "positive integers"),
            ({"models": ["M", "M"]}, "repeat"),
            ({"throughput_task": ["math", "poem"]}, "must be one of"),
            ({"record_text": "yes"}, "true or false"),
            ({"unknown_key": 1}, "unknown key"),
        ):
            with self.subTest(values=values), self.assertRaisesRegex(settings.ConfigError, message):
                self._load(self._bench(**values))

    def test_gpu_runs_require_a_budget_and_cpu_runs_do_not(self):
        raw = _raw()
        del raw["benchmark"]["gpu_memory_budget_gb"]
        with self.assertRaisesRegex(settings.ConfigError, "required for GPU"):
            self._load(raw)
        self.assertIsNone(self._load(raw, "--device", "CPU", "--profiles", "paged_min")["gpu_memory_budget_gb"])

    def test_devices_are_cpu_or_gpu_selectors(self):
        for device in ("NPU", "AUTO", "HETERO:GPU,CPU"):
            with self.subTest(device=device), self.assertRaisesRegex(settings.ConfigError, "CPU or GPU"):
                self._load(_raw(), "--device", device)
        self.assertEqual(self._load(_raw(), "--device", "gpu.1")["device"], "GPU.1")

    def test_profile_structure(self):
        for mutate, message in (
            (lambda p: p.update(cache_size=8), "belongs under `scheduler`"),
            (lambda p: p.update(ov=[]), "must be mappings"),
            (lambda p: p.update(scheduler=""), "must be mappings"),
            (lambda p: p.update(name=""), "non-empty"),
            (lambda p: p["ov"].update(draft_model="x"), "not a plugin property"),
            (lambda p: p["ov"].update(X=None), "string, number or boolean"),
            (lambda p: p["scheduler"].update(enable_prefix_caching=True), "prefix caching"),
            (lambda p: p["scheduler"].pop("max_num_seqs"), "lacks max_num_seqs"),
            (lambda p: p["scheduler"].update(cache_size=2.5), "integer"),
        ):
            raw = _raw()
            mutate(raw["profiles"][0])
            with self.subTest(message=message), self.assertRaisesRegex(settings.ConfigError, message):
                self._load(raw)
        raw = _raw()
        raw["profiles"].append(copy.deepcopy(raw["profiles"][0]))
        with self.assertRaisesRegex(settings.ConfigError, "unique"):
            self._load(raw)

    def test_overrides_are_validated_after_merging(self):
        with self.assertRaisesRegex(settings.ConfigError, "prefix caching"):
            self._load(_raw(), "--scheduler-config", "enable_prefix_caching=1")
        with self.assertRaisesRegex(settings.ConfigError, "lacks max_num_batched_tokens"):
            self._load(_raw(), "--scheduler-config", "max_num_batched_tokens=")
        with self.assertRaisesRegex(settings.ConfigError, "KEY=VALUE"):
            self._load(_raw(), "--pipeline-config", "NOVALUE")
        resolved = self._load(_raw(), "--profiles", "paged_min",
                              "--pipeline-config", "KV_CACHE_PRECISION=u8", "ATTENTION_BACKEND=")
        self.assertEqual(resolved["profiles"][0]["ov"], {"KV_CACHE_PRECISION": "u8"})
        self.assertEqual(self._load(_raw(), "--scheduler-config", "cache_size=auto")["profiles"][0]["cache_pool"], "auto")

    def test_an_override_that_changes_the_pipeline_is_announced(self):
        raw = _raw()
        raw["profiles"] = [{"name": "stateful", "ov": {}}]
        with self.assertRaisesRegex(settings.ConfigError, "lacks"):
            self._load(raw, "--scheduler-config", "cache_size=8")
        resolved = self._load(raw, "--scheduler-config", "max_num_batched_tokens=4096", "max_num_seqs=1",
                              "enable_prefix_caching=false")
        self.assertEqual(resolved["profiles"][0]["pipeline_mode"], settings.PIPELINE_PAGED)
        self.assertTrue(any("moved profile" in n for n in resolved["notices"]))

    def test_candidate_count_override_changes_only_speculative_profiles(self):
        resolved = self._load(_raw(), "--assistant-tokens", "5")
        plain, swept = resolved["profiles"]
        self.assertEqual(plain["mtp"], dict(settings.SPECULATIVE_OFF))
        self.assertEqual(swept["mtp"]["num_assistant_tokens"], 5)
        self.assertEqual(self._load(_raw(), "--assistant-tokens", "2")["profiles"][1]["mtp"]["num_assistant_tokens"], 2)

    def test_speculative_rules(self):
        cases = [
            (lambda p: p.update(dflash={"model": "d", "num_assistant_tokens": 0}), "integer >= 1"),
            (lambda p: p.update(dflash={"model": "d", "num_assistant_tokens": True}), "integer >= 1"),
            (lambda p: p.update(dflash={"model": "d", "num_assistant_tokens": 3, "k": 3}), "unknown key"),
            (lambda p: p.update(dflash=True), "must be a mapping"),
            # MTP is not supported: the section is an unknown profile key.
            (lambda p: p.update(mtp={"num_assistant_tokens": 3}), r"unknown key\(s\): mtp"),
            (lambda p: (p.update(dflash={"model": "d", "num_assistant_tokens": 3}), p.pop("scheduler")),
             "stateful pipeline"),
            (lambda p: (p.update(dflash={"model": "d", "num_assistant_tokens": 9}),
                        p["scheduler"].update(max_num_batched_tokens=3)), "max_num_batched_tokens=3"),
            # The draft always runs on the target's device.
            (lambda p: p.update(dflash={"model": "d", "num_assistant_tokens": 3, "device": "CPU"}),
             r"unknown key\(s\): device"),
            (lambda p: p.update(dflash={"num_assistant_tokens": 7}), "dflash.model"),
            (lambda p: (p.update(dflash={"model": "d", "num_assistant_tokens": 7}),
                        p["scheduler"].update(num_linear_attention_blocks=8)), "num_linear_attention_blocks=8"),
            # Eagle3 shares the section schema; a profile speculates with one strategy only.
            (lambda p: p.update(eagle3={"model": "d", "num_assistant_tokens": 0}), "eagle3.num_assistant_tokens"),
            (lambda p: p.update(eagle3=["d"]), "eagle3 must be a mapping"),
            (lambda p: p.update(eagle3={"num_assistant_tokens": 4}), "eagle3.model"),
            (lambda p: (p.update(eagle3={"model": "d", "num_assistant_tokens": 4}), p.pop("scheduler")),
             "enables eagle3 on the stateful pipeline"),
            (lambda p: p.update(dflash={"model": "d", "num_assistant_tokens": 3},
                                eagle3={"model": "e", "num_assistant_tokens": 3}),
             "at most one strategy"),
        ]
        for mutate, message in cases:
            raw = _raw()
            mutate(raw["profiles"][0])
            with self.subTest(message=message), self.assertRaisesRegex(settings.ConfigError, message):
                self._load(raw)

    def test_speculative_normalization_and_disabled_sections(self):
        raw = _raw()
        raw["profiles"][0]["dflash"] = {"model": "models/d", "num_assistant_tokens": 7}
        raw["profiles"][1]["dflash"] = {"enabled": False, "model": "models/d", "num_assistant_tokens": 3}
        dflash, disabled = self._load(raw)["profiles"]
        self.assertEqual(dflash["mtp"], {"enabled": True, "strategy": "dflash", "model": "models/d",
                                         "num_assistant_tokens": 7})
        self.assertEqual(disabled["mtp"], dict(settings.SPECULATIVE_OFF))

    def test_eagle3_section_resolves_to_its_own_strategy(self):
        raw = _raw()
        raw["profiles"][1].pop("dflash", None)
        raw["profiles"][1]["eagle3"] = {"model": "models/e", "num_assistant_tokens": 4}
        _, eagle3 = self._load(raw)["profiles"]
        self.assertEqual(eagle3["mtp"], {"enabled": True, "strategy": "eagle3", "model": "models/e",
                                         "num_assistant_tokens": 4})
        self.assertEqual(metrics.strategy_label({"speculative_strategy": "eagle3"}), "Eagle3")
        self.assertEqual(metrics.strategy_label({"speculative_strategy": "dflash"}), "dFlash")

    def test_profile_filter_and_empty_matrix(self):
        with self.assertRaisesRegex(settings.ConfigError, "unknown profile"):
            self._load(_raw(), "--profiles", "nope")
        resolved = self._load(self._bench(context_tokens=[0], throughput_task="summary_2s"))
        with self.assertRaisesRegex(settings.ConfigError, "matrix is empty"):
            settings.plan_run(resolved)

    def test_context_zero_runs_only_standalone_tasks(self):
        resolved = self._load(self._bench(context_tokens=[8000, 0], throughput_task=["code", "summary_2s", "chat"]))
        self.assertEqual(resolved["context_tokens"], (0, 8000))
        self.assertEqual(settings.run_tasks(resolved, 0), ["code", "chat"])
        self.assertEqual(settings.run_tasks(resolved, 8000), ["code", "summary_2s", "chat"])
        self.assertEqual(len(settings.plan_run(resolved)), 2 * 5)

    def test_smoke_runs_are_labelled(self):
        self.assertTrue(self._load(_raw(), "--warmup", "0")["smoke_test"])
        self.assertFalse(self._load(_raw())["smoke_test"])

    def test_yaml_is_loaded_strictly(self):
        with tempfile.TemporaryDirectory() as root:
            for text, message in (("a: 1\na: 2\n", "duplicate key"), ("- 1\n", "mapping at the top"),
                                  ("a: .nan\n", "finite"), ("a: 2026-01-01\n", "unsupported YAML type"),
                                  ("a: [\n", "not valid YAML"), ("1: x\n", "strings")):
                path = Path(root, "c.yaml")
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaisesRegex(settings.ConfigError, message):
                    settings.load_yaml(path)

    def test_anchored_profiles_are_independent_copies(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "c.yaml")
            path.write_text("profiles:\n  - {name: a, ov: &o {X: '1'}}\n  - {name: b, ov: *o}\n", encoding="utf-8")
            data = settings.load_yaml(path)
        data["profiles"][0]["ov"]["Y"] = "2"
        self.assertNotIn("Y", data["profiles"][1]["ov"])

    def test_speculative_reference_is_display_only_and_validated(self):
        reference = settings._resolve_reference({"source": "blog", "num_assistant_tokens": 7,
                                                 "tasks": {"code": {"dataset": "HumanEval",
                                                                    "acceptance_length": 6.4, "speedup": 2.2}}})
        self.assertEqual(reference["tasks"]["code"]["acceptance_length"], 6.4)
        for broken, message in (
            ({"num_assistant_tokens": 0, "tasks": {"code": {}}}, "positive integer"),
            ({"num_assistant_tokens": 7, "tasks": {"poem": {}}}, "not a throughput task"),
            ({"num_assistant_tokens": 7, "tasks": {"code": {"acceptance_length": 6.4}}}, "speedup"),
            ({"num_assistant_tokens": 7, "tasks": {"code": {"acceptance_length": 1, "speedup": 1, "x": 1}}}, "unknown"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(settings.ConfigError, message):
                settings._resolve_reference(broken)


class TestReportRendering(unittest.TestCase):
    def test_unavailable_values_render_as_dashes_and_failures_stay_visible(self):
        snapshot = {
            "run_id": "r", "revision": 3, "completed": True,
            "settings": {"models": ["m"], "context_tokens": [8000], "device": "CPU", "warmup": 1,
                         "iterations": 3, "output_tokens": 64, "max_system_memory_pct": 80.0,
                         "gpu_memory_budget_gb": None, "record_text": False},
            "cases": [_case("paged_min", None, ttft=None, context_tokens=8000, status="crashed",
                            error={"category": "crashed", "stage": "prefill", "message": "child exited",
                                   "exit_code": 3221226505})],
        }
        text = "\n".join(report.render_report(snapshot))
        self.assertIn("Run       : r revision 3", text)
        self.assertIn("crashed in prefill", text)
        self.assertIn("| crashed | 0 | -- | -- | -- | -- | -- | -- |", text)
        self.assertNotIn("nan", text.lower())
        self.assertNotIn("None GiB", text)


if __name__ == "__main__":
    unittest.main()

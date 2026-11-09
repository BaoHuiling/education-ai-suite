# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""The cross-model evaluation: its plan resolves through the benchmark's own settings, every
profile of every config runs, speculative off/on share one invocation, and the Markdown report renders from results.json snapshots.
Hardware-independent: benchmark invocations are replaced by fixtures."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from components.context_bench import evaluate, evidence, settings


def _resolve(invocation):
    args = list(invocation["args"])
    args[args.index("--config") + 1] = str(settings.SC_ROOT / args[args.index("--config") + 1])
    return settings._load_settings(settings._parse_args(args))


def _case(case_id, profile, task="code", context=8000, status="ok", tpot=40.0, **extra):
    case = {"case_id": case_id, "label": f"model | {profile} | {context} | {task}",
            "mode": "throughput", "model": "Qwen3.6-35B-A3B", "profile": profile,
            "context_tokens": context, "throughput_task": task, "status": status,
            "other_tokens_avg_latency": tpot, "first_token_latency": 2000.0,
            "decode_throughput": 1000.0 / tpot if tpot else None, "latency": tpot,
            "comparison_key": f"{context}-{task}", "output_sha256": "same",
            "mtp": profile != "paged_min", "warnings": [], "flags": []}
    if case["mtp"]:
        case.update(num_assistant_tokens=3, tokens_per_step=2.5, speculative_strategy="dflash")
    case.update(extra)
    return case


def _snapshot(cases, plan_state="done", warnings=(), accuracy_settings=None):
    return {
        "environment": {"processor": "cpu", "memory_gb": 64, "device_names": ["iGPU"],
                        "packages": {"openvino": "2026.4.1"}},
        "settings": {"context_tokens": [8000], "throughput_tasks": ["code"], "output_tokens": 512,
                     "warmup": 1, "iterations": 3, "accuracy": accuracy_settings},
        "counts": {"planned": len(cases), "ok": sum(c["status"] == "ok" for c in cases),
                   "by_status": {"ok": sum(c["status"] == "ok" for c in cases)}},
        "plan": [{"case_id": c["case_id"], "label": c["label"], "state": plan_state,
                  "planned_probes": 32} for c in cases],
        "cases": cases, "warnings": list(warnings), "similarity_notes": [],
    }


def _write_run(evaluation_dir: Path, model: str, suite: str, snapshot: dict) -> Path:
    run_dir = evaluation_dir / model / suite / "20261020T000000Z-abcd0123"
    run_dir.mkdir(parents=True)
    (run_dir / evidence.RESULTS_NAME).write_text(json.dumps(snapshot), encoding="utf-8")
    return run_dir


class TestPlan(unittest.TestCase):
    # By default every test case of every config runs.
    EVERY_PROFILE = {
        "qwen3-vl-8b": ["paged_min", "eagle3_k3", "eagle3_k4", "eagle3_k5"],
        "qwen3.5-9b": ["paged_min", "dflash_k3", "dflash_k5", "dflash_k7", "dflash_k15"],
        "qwen3.6-35b-a3b": ["paged_min", "dflash_k3", "dflash_k5", "dflash_k7", "dflash_k15"],
        "qwen3.8-27b": ["paged_min"],
    }

    def test_every_model_and_suite_is_one_invocation_in_model_order(self):
        planned = evaluate.plan_invocations(evaluate.MODEL_KEYS, evaluate.SUITES)
        self.assertEqual([(i["model"], i["suite"]) for i in planned],
                         [(m, s) for m in evaluate.MODEL_KEYS for s in evaluate.SUITES])
        for invocation in planned:
            args = invocation["args"]
            self.assertEqual(args[args.index("--weight-format") + 1], "int4")
            self.assertEqual(invocation["profiles"], self.EVERY_PROFILE[invocation["model"]])

    def test_every_invocation_resolves_through_the_benchmark_settings(self):
        """The same validation --list-profiles runs: configs, profiles and overrides agree."""
        for quick in (False, True):
            for invocation in evaluate.plan_invocations(evaluate.MODEL_KEYS, evaluate.SUITES,
                                                        quick=quick):
                with self.subTest(model=invocation["model"], suite=invocation["suite"], quick=quick):
                    resolved = _resolve(invocation)
                    self.assertEqual(resolved["weight_format"], "int4")
                    self.assertEqual([p["name"] for p in resolved["profiles"]], invocation["profiles"])
                    self.assertEqual(resolved["warmup"], 0 if quick else 1)
                    self.assertEqual(resolved["iterations"], 1 if quick else 3)
                    timed = not quick and invocation["suite"] != evaluate.SUITE_ACCURACY
                    self.assertEqual(resolved["iteration_gap_sec"], 30.0 if timed else 0.0)
                    self.assertEqual(bool(resolved.get("accuracy")),
                                     invocation["suite"] == evaluate.SUITE_ACCURACY)
                    self.assertTrue(settings.plan_run(resolved))

    def test_an_explicit_iteration_gap_applies_to_every_suite(self):
        for invocation in evaluate.plan_invocations(["qwen3-vl-8b"], evaluate.SUITES,
                                                    iteration_gap=0.0):
            with self.subTest(suite=invocation["suite"]):
                self.assertEqual(_resolve(invocation)["iteration_gap_sec"], 0.0)

    def test_all_models_share_one_workload(self):
        planned = evaluate.plan_invocations(evaluate.MODEL_KEYS, [evaluate.SUITE_THROUGHPUT])
        workloads = {tuple(i["args"][i["args"].index("--contexts"):]) for i in planned}
        self.assertEqual(len(workloads), 1)
        resolved = _resolve(planned[0])
        self.assertEqual(resolved["context_tokens"], (0, 8000))
        self.assertEqual(resolved["throughput_tasks"], ("code", "math", "chat", "summary_2s"))
        self.assertEqual(resolved["output_tokens"], 512)

    def test_speculative_off_and_on_run_matched_in_one_invocation(self):
        for model, windows, expected, strategy in (
                ("qwen3.6-35b-a3b", [3, 7], ["paged_min", "dflash_k3", "dflash_k7"], "dflash"),
                ("qwen3.5-9b", [5, 15], ["paged_min", "dflash_k5", "dflash_k15"], "dflash"),
                ("qwen3-vl-8b", [4], ["paged_min", "eagle3_k4"], "eagle3")):
            with self.subTest(model=model):
                invocation = evaluate.plan_invocations([model], [evaluate.SUITE_THROUGHPUT],
                                                       windows=windows)[0]
                profiles = _resolve(invocation)["profiles"]
                self.assertEqual([p["name"] for p in profiles], expected)
                self.assertEqual([p["mtp"]["strategy"] for p in profiles],
                                 [None] + [strategy] * len(windows))
                self.assertEqual([p["mtp"]["num_assistant_tokens"] for p in profiles[1:]], windows)
                for profile in profiles[1:]:
                    self.assertEqual((profile["ov"], profile["scheduler"]),
                                     (profiles[0]["ov"], profiles[0]["scheduler"]))

    def test_a_window_filter_keeps_plain_models_and_shipped_windows_only(self):
        planned = evaluate.plan_invocations(evaluate.MODEL_KEYS, [evaluate.SUITE_THROUGHPUT],
                                            windows=[5])
        self.assertEqual({i["model"]: i["profiles"] for i in planned}, {
            "qwen3-vl-8b": ["paged_min", "eagle3_k5"], "qwen3.5-9b": ["paged_min", "dflash_k5"],
            "qwen3.6-35b-a3b": ["paged_min", "dflash_k5"], "qwen3.8-27b": ["paged_min"]})

    def test_an_unshipped_window_is_an_argument_error(self):
        with self.assertRaisesRegex(evaluate.EvaluationError, r"k=\[4\]"):
            evaluate.plan_invocations(["qwen3.6-35b-a3b"], evaluate.SUITES, windows=[4])
        # Shipped for Eagle3 only, so valid as soon as its model is selected.
        evaluate.plan_invocations(["qwen3.6-35b-a3b", "qwen3-vl-8b"], evaluate.SUITES, windows=[4])
        self.assertEqual(evaluate.main(["--models", "qwen3.8-27b", "--spec-k", "5",
                                        "--list-profiles"]), evaluate.EXIT_CONFIG)

    def test_device_is_forwarded_only_when_given(self):
        default = evaluate.plan_invocations(["qwen3-vl-8b"], [evaluate.SUITE_CLASSROOM])[0]
        cpu = evaluate.plan_invocations(["qwen3-vl-8b"], [evaluate.SUITE_CLASSROOM], device="CPU")[0]
        self.assertNotIn("--device", default["args"])
        self.assertEqual(_resolve(cpu)["device"], "CPU")


class TestRunAndReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _fake_stream(self, snapshots, exit_codes):
        def stream(command, log_path):
            output_dir = Path(command[command.index("--output-dir") + 1])
            key = (output_dir.parent.name, output_dir.name)
            log_path.write_text("log\n", encoding="utf-8")
            if key in snapshots:
                _write_run(output_dir.parent.parent, *key, snapshots[key])
            return exit_codes.get(key, 0)
        return stream

    def test_a_sweep_renders_dflash_speedup_accuracy_and_failures(self):
        retrieval = {"recall_rate": 0.9, "exact_match_rate": 0.8, "token_f1": 0.85,
                     "distractor_rate": 0.0, "probe_count": 28,
                     "per_task": {"niah_single": {"recall_rate": 1.0}}}
        accuracy_settings = {"suites": ["retrieval"], "retrieval": {"tasks": ["niah_single"]}}
        snapshots = {
            ("qwen3.6-35b-a3b", "throughput"): _snapshot([
                _case("c01", "paged_min", tpot=40.0), _case("c02", "dflash_k3", tpot=20.0)]),
            ("qwen3.6-35b-a3b", "accuracy"): _snapshot([
                _case("c01", "paged_min", task=None, mode="accuracy",
                      accuracy={"retrieval": retrieval})], accuracy_settings=accuracy_settings),
            ("qwen3.8-27b", "throughput"): _snapshot([
                _case("c01", "paged_min", status="missing_ir", tpot=None,
                      error={"category": "missing_ir", "message": "prepare a|b"})]),
        }
        invocations = evaluate.plan_invocations(["qwen3.6-35b-a3b", "qwen3.8-27b"],
                                                [evaluate.SUITE_THROUGHPUT, evaluate.SUITE_ACCURACY])
        codes = {("qwen3.8-27b", "throughput"): 1}
        with mock.patch.object(evaluate, "_stream", self._fake_stream(snapshots, codes)):
            code = evaluate.run_evaluation(invocations, self.root, {"weight_format": "int4"})
        self.assertEqual(code, evaluate.EXIT_FAILURE)  # 27B failed and its accuracy run wrote nothing

        manifest = json.loads((self.root / evaluate.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertTrue(manifest["completed"])
        self.assertEqual([r["exit_code"] for r in manifest["runs"]], [0, 0, 1, 0])
        text = (self.root / evaluate.REPORT_NAME).read_text(encoding="utf-8")
        self.assertIn("## Speculative decoding: off vs on", text)
        self.assertIn("| dFlash | 3 |", text)
        self.assertIn("| 2.00x |", text)  # 40 ms plain / 20 ms DFlash
        self.assertIn("| identical |", text)
        self.assertIn("## Accuracy", text)
        self.assertIn("| 90% |", text)
        self.assertIn("**missing_ir** missing_ir: prepare a|b", text)
        # Only in the model comparison, not as an empty measurement row.
        self.assertNotIn("| qwen3.8-27b | paged_min | 8,000 tokens | code | missing_ir |", text)
        self.assertIn("| 8,000 tokens | code | 25.00 (2.00) | missing_ir |", text)
        self.assertIn("qwen3.8-27b / accuracy: no evidence written", text)
        self.assertIn("(qwen3.6-35b-a3b/throughput/20261020T000000Z-abcd0123/report.txt)", text)

    def test_the_published_reference_is_compared_at_its_own_window(self):
        reference = {"source": "blog", "num_assistant_tokens": 7,
                     "tasks": {"code": {"dataset": "HumanEval", "acceptance_length": 6.4,
                                        "speedup": 2.2, "throughput": 89.8}}}
        snapshot = _snapshot([
            _case("c01", "paged_min", context=0, tpot=22.0),
            _case("c02", "dflash_k3", context=0, tpot=12.0),
            _case("c03", "dflash_k7", context=0, tpot=10.0, num_assistant_tokens=7,
                  tokens_per_step=6.0)])
        snapshot["settings"]["speculative_reference"] = reference
        _write_run(self.root, "qwen3.6-35b-a3b", evaluate.SUITE_THROUGHPUT, snapshot)
        evidence.atomic_write_json(str(self.root / evaluate.MANIFEST_NAME), {
            "evaluation_id": "x", "options": {},
            "runs": [{"model": "qwen3.6-35b-a3b", "suite": evaluate.SUITE_THROUGHPUT}]})
        text = (evaluate.write_report(self.root)).read_text(encoding="utf-8")
        section = text[text.index("## Speculative decoding against the published reference"):]
        self.assertIn("### qwen3.6-35b-a3b, no transcript (task prompt only)", section)
        # k=7 is the measured column (22 / 10 ms), not k=3's 1.83x.
        self.assertIn("| code | HumanEval | 6.40 | 6.00 | 2.20x | 2.20x |", section)

    def test_all_ok_is_exit_zero_and_ctrl_c_stops_the_sweep(self):
        snapshot = _snapshot([_case("c01", "paged_min")])
        invocations = evaluate.plan_invocations(["qwen3-vl-8b"], evaluate.SUITES)
        snapshots = {("qwen3-vl-8b", s): snapshot for s in evaluate.SUITES}
        with mock.patch.object(evaluate, "_stream", self._fake_stream(snapshots, {})):
            self.assertEqual(evaluate.run_evaluation(invocations, self.root / "ok", {}),
                             evaluate.EXIT_OK)
        (self.root / "stop").mkdir()
        codes = {("qwen3-vl-8b", "throughput"): evaluate.EXIT_INTERRUPTED}
        with mock.patch.object(evaluate, "_stream", self._fake_stream(snapshots, codes)):
            self.assertEqual(evaluate.run_evaluation(invocations, self.root / "stop", {}),
                             evaluate.EXIT_INTERRUPTED)
        manifest = json.loads((self.root / "stop" / evaluate.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertTrue(manifest["interrupted"])
        self.assertEqual(len(manifest["runs"]), 1)
        self.assertTrue((self.root / "stop" / evaluate.REPORT_NAME).is_file())

    def test_report_only_re_renders_an_existing_evaluation(self):
        self.assertEqual(evaluate.main(["--report", str(self.root)]), evaluate.EXIT_CONFIG)
        evidence.atomic_write_json(str(self.root / evaluate.MANIFEST_NAME),
                                   {"evaluation_id": "x", "runs": [], "options": {}})
        self.assertEqual(evaluate.main(["--report", str(self.root)]), evaluate.EXIT_OK)
        self.assertIn("# Context Bench Model Evaluation",
                      (self.root / evaluate.REPORT_NAME).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

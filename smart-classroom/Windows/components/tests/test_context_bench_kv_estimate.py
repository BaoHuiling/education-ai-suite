# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Cache-demand estimate, scheduler pool preflight and the shipped configuration invariants.

The estimate sizes `cache_size: auto` and validates fixed pools before a multi-gigabyte load,
so it must never underestimate: unknown contributions make it unavailable, an infeasible or
unknown `auto` is a case error, and nothing silently clamps a pool or changes the backend.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from components.context_bench import benchmark, settings, sizing
from components.context_bench.sizing import (
    auto_cache_size_gb,
    check_ir_layout,
    estimate_cache,
    fixed_state_cache_bytes,
    ir_weight_precision,
    theoretical_kv_bytes_per_token,
    validate_fixed_cache_size,
)

# Qwen3.5-9B-like: 32 layers, 3x linear + 1x full attention, so 8 grow with context.
HYBRID = {"text_config": {"num_hidden_layers": 32, "num_key_value_heads": 4, "head_dim": 256,
                          "layer_types": (["linear_attention"] * 3 + ["full_attention"]) * 8,
                          "vocab_size": 1000}}
DENSE = {"num_hidden_layers": 8, "num_key_value_heads": 4, "head_dim": 128, "vocab_size": 1000}
FIXED = 53477376  # 51 MiB of linear-attention state, as read from a Qwen3.5-9B IR
GIB = 1024 ** 3


def _raw(**benchmark_overrides):
    return {
        "model": {"provider": "openvino", "device": "GPU", "weight_format": "int4",
                  "models_base_path": "models"},
        "benchmark": {"models": ["M"], "context_tokens": [8000], "output_tokens": 64,
                      "warmup": 1, "iterations": 3, "timeout_sec": 600, "gpu_memory_budget_gb": 59,
                      "output_dir": "monitoring/executionlogs/context_bench/test",
                      **benchmark_overrides},
        "profiles": [{"name": "paged", "ov": {"KV_CACHE_PRECISION": "f16"},
                      "scheduler": {"max_num_batched_tokens": 8192, "max_num_seqs": 1,
                                    "enable_prefix_caching": False}}],
    }


def _resolve(raw, *argv):
    return settings._resolve_settings(raw, settings._parse_args(list(argv)))


class TestKvBytesPerToken(unittest.TestCase):
    def test_dense_model_counts_every_layer(self):
        self.assertEqual(theoretical_kv_bytes_per_token(DENSE), 2 * 8 * 4 * 128 * 2)

    def test_hybrid_model_counts_only_full_attention_layers(self):
        self.assertEqual(theoretical_kv_bytes_per_token(HYBRID), 2 * 8 * 4 * 256 * 2)

    def test_nested_vlm_text_config_is_read(self):
        config = {"vision_config": {"hidden_size": 1152},
                  "text_config": {"num_hidden_layers": 28, "num_key_value_heads": 4, "head_dim": 128}}
        self.assertEqual(theoretical_kv_bytes_per_token(config), 2 * 28 * 4 * 128 * 2)

    def test_malformed_metadata_is_unknown_not_an_underestimate(self):
        for config in (
            {"text_config": {"num_hidden_layers": 32, "head_dim": 128}},
            {**DENSE, "layer_types": ["full_attention"] * 2},
            {**DENSE, "layer_types": ["linear_attention"] * 8},
            {**DENSE, "num_key_value_heads": True},
            {**DENSE, "head_dim": "128"},
        ):
            with self.subTest(config=config):
                self.assertIsNone(theoretical_kv_bytes_per_token(config))

    def test_quantized_kv_carries_per_row_overhead(self):
        self.assertEqual(theoretical_kv_bytes_per_token(DENSE, 1, 4), 2 * 8 * 4 * (128 + 4))
        self.assertEqual(sizing.kv_quantization_param_bytes({"KV_CACHE_PRECISION": "u8"}), 4)
        self.assertEqual(sizing.kv_quantization_param_bytes({"KV_CACHE_PRECISION": "f16"}), 0)
        self.assertEqual(sizing.kv_cache_dtype_bytes({"KV_CACHE_PRECISION": "u4"}), 0.5)

    def test_unknown_kv_precision_makes_the_estimate_unavailable(self):
        detail = estimate_cache(DENSE, 0, {"KV_CACHE_PRECISION": "q3"}, 1000)
        self.assertIsNone(detail["gib"])
        self.assertIn("no known size", detail["reason"])


class TestReservations(unittest.TestCase):
    def test_fixed_state_is_reserved_per_row(self):
        one = estimate_cache(HYBRID, FIXED, {}, 160000, state_rows=1)["gib"]
        many = estimate_cache(HYBRID, FIXED, {}, 160000, state_rows=256)["gib"]
        self.assertAlmostEqual(many - one, 255 * FIXED / GIB, places=6)
        self.assertGreater(many, 17)

    def test_rows_follow_linear_attention_blocks_then_sequences(self):
        self.assertEqual(sizing.state_rows({}), 1)
        self.assertEqual(sizing.state_rows({"max_num_seqs": 2}), 2)
        self.assertEqual(sizing.state_rows({"max_num_seqs": 1, "num_linear_attention_blocks": 17}), 17)

    def test_hybrid_model_without_readable_state_is_unknown(self):
        for fixed in (0, None):
            with self.subTest(fixed=fixed):
                self.assertIsNone(estimate_cache(HYBRID, fixed, {}, 8000)["gib"])

    def test_dense_model_is_unaffected_by_rows(self):
        self.assertEqual(estimate_cache(DENSE, 0, {}, 8000, state_rows=256)["gib"],
                         estimate_cache(DENSE, 0, {}, 8000, state_rows=1)["gib"])

    def test_context_zero_is_unavailable_never_zero_tokens(self):
        detail = estimate_cache(DENSE, 0, {}, None)
        self.assertIsNone(detail["gib"])
        self.assertIn("standalone prompt length", detail["reason"])

    def test_external_draft_kv_is_included_and_window_capped(self):
        draft = {"num_hidden_layers": 2, "num_key_value_heads": 4, "head_dim": 128}
        windowed = {**draft, "sliding_window": 4096}
        plain = estimate_cache(DENSE, 0, {}, 32000)["gib"]
        full = estimate_cache(DENSE, 0, {}, 32000, draft_config=draft, speculative=True)["gib"]
        capped = estimate_cache(DENSE, 0, {}, 32000, draft_config=windowed, speculative=True)["gib"]
        per_token = theoretical_kv_bytes_per_token(draft)
        self.assertAlmostEqual(full - plain, per_token * 32000 / GIB, places=6)
        self.assertAlmostEqual(capped - plain, per_token * 4096 / GIB, places=6)
        self.assertIsNone(estimate_cache(DENSE, 0, {}, 32000, draft_config=None, speculative=True)["gib"])

    def test_draft_sliding_layers_are_counted_and_only_they_are_window_capped(self):
        # The shipped Qwen3.6 DFlash draft: five sliding-window layers and one full layer.
        draft = {"num_hidden_layers": 6, "num_key_value_heads": 8, "head_dim": 128,
                 "sliding_window": 4096,
                 "layer_types": ["sliding_attention"] * 5 + ["full_attention"]}
        per_layer = 2 * 8 * 128 * 2
        detail = estimate_cache(DENSE, 0, {}, 8516, draft_config=draft, speculative=True)
        self.assertEqual(detail["speculative_bytes"], per_layer * (8516 + 5 * 4096))
        # Below the window every layer holds every token.
        self.assertEqual(sizing.draft_kv_bytes(draft, 1000), per_layer * 6 * 1000)
        for bad in ({**draft, "layer_types": ["sliding_attention"] * 5},
                    {**draft, "layer_types": ["linear_attention"] * 5 + ["full_attention"]}):
            with self.subTest(layer_types=bad["layer_types"]):
                self.assertIsNone(estimate_cache(DENSE, 0, {}, 8516, draft_config=bad,
                                                 speculative=True)["gib"])

    def test_expected_kv_keeps_the_reference_shape(self):
        kv = estimate_cache(HYBRID, FIXED, {"KV_CACHE_PRECISION": "f16"}, 160000)["gib"]
        self.assertAlmostEqual(kv, (2 * 8 * 4 * 256 * 2 * 160000 + FIXED) / GIB)


class TestPoolSizing(unittest.TestCase):
    def test_auto_formula_and_ceiling(self):
        sized = auto_cache_size_gb(4.9, weight_gib=8.8, budget_gb=59)
        self.assertEqual(sized["status"], "ok")
        self.assertEqual(sized["pool"], 7)  # ceil(1.3 * 4.9)
        self.assertEqual(sized["ceiling"], 42)  # floor(59 - 8.8 - 8)
        self.assertIsInstance(sized["pool"], int)
        self.assertEqual(auto_cache_size_gb(0.01, 1.0, 59)["pool"], 2)

    def test_an_infeasible_pool_is_reported_not_clamped(self):
        sized = auto_cache_size_gb(40.0, weight_gib=33.0, budget_gb=59)
        self.assertEqual(sized["status"], "infeasible")
        self.assertGreater(sized["pool"], sized["ceiling"])

    def test_an_unknown_estimate_cannot_be_sized(self):
        self.assertEqual(auto_cache_size_gb(None, 8.8, 59)["status"], "unknown")
        self.assertEqual(auto_cache_size_gb(1.0, 8.8, None)["status"], "unknown")

    def test_fixed_pool_validation(self):
        validate_fixed_cache_size(3, 1.61)
        with self.assertRaisesRegex(ValueError, "below the estimated 1.61 GB"):
            validate_fixed_cache_size(1, 1.61)
        validate_fixed_cache_size("auto", 1.61)
        validate_fixed_cache_size(1, None)


def _write_ir(directory: Path, config: dict, layout: str = "llm", state_xml: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    graphs = (["openvino_language_model", "openvino_text_embeddings_model",
               "openvino_vision_embeddings_model"] if layout == "vlm" else ["openvino_model"])
    for name in graphs + ["openvino_tokenizer", "openvino_detokenizer"]:
        body = state_xml if name in ("openvino_model", "openvino_language_model") else ""
        (directory / f"{name}.xml").write_text(f"<net><layers>{body}</layers></net>", encoding="utf-8")
        (directory / f"{name}.bin").write_bytes(b"\0" * 1024)
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (directory / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    return directory


class TestCasePreflight(unittest.TestCase):
    """Fixed pools are validated before loading; `auto` is resolved or refused per case."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.model_dir = _write_ir(self.root / "target", DENSE)

    def tearDown(self):
        self._tmp.cleanup()

    def _preflight(self, raw, *argv):
        raw = copy.deepcopy(raw)
        raw["model"]["model_dirs"] = {"M": str(self.model_dir)}
        resolved = _resolve(raw, *argv)
        planned = settings.plan_run(resolved)[0]
        static = benchmark._model_static(resolved, "M", self.model_dir,
                                         benchmark.evidence.PathRedactor(settings.SC_ROOT))
        return resolved, planned, benchmark._preflight_case(planned, resolved, static)

    def _scheduler(self, **extra):
        raw = _raw()
        raw["profiles"][0]["scheduler"].update(extra)
        return raw

    def test_a_fixed_pool_large_enough_passes_and_is_kept(self):
        _, _, prepared = self._preflight(self._scheduler(cache_size=2))
        self.assertEqual(prepared["scheduler"]["cache_size"], 2)
        self.assertIsNotNone(prepared["estimate"]["gib"])

    def test_a_fixed_pool_below_demand_is_a_case_error(self):
        raw = self._scheduler(cache_size=1)
        raw["benchmark"]["context_tokens"] = [2_000_000]
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(raw)
        self.assertEqual(caught.exception.category, "cache_too_small")

    def test_auto_resolves_to_a_whole_pool_within_the_budget(self):
        _, _, prepared = self._preflight(self._scheduler(cache_size="auto"))
        self.assertEqual(prepared["scheduler"]["cache_size"], 2)
        self.assertEqual(prepared["ceiling_gb"], 50)

    def test_infeasible_auto_is_an_error_not_a_clamp_or_backend_change(self):
        raw = self._scheduler(cache_size="auto")
        raw["benchmark"]["gpu_memory_budget_gb"] = 9
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(raw)
        self.assertEqual(caught.exception.category, "cache_infeasible")
        # The requested profile is still paged with `auto`: nothing was removed.
        profile = _resolve(dict(raw, model={**raw["model"], "model_dirs": {"M": str(self.model_dir)}}))["profiles"][0]
        self.assertEqual(profile["scheduler"]["cache_size"], "auto")
        self.assertEqual(profile["pipeline_mode"], settings.PIPELINE_PAGED)

    def test_unknown_architecture_auto_requires_an_explicit_pool(self):
        (self.model_dir / "config.json").write_text(json.dumps({"num_hidden_layers": 4}), encoding="utf-8")
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(self._scheduler(cache_size="auto"))
        self.assertEqual(caught.exception.category, "cache_unknown")

    def test_cpu_auto_requires_an_explicit_pool(self):
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(self._scheduler(cache_size="auto"), "--device", "CPU")
        self.assertIn("CPU", caught.exception.message)

    def test_context_zero_fixed_pool_is_kept_with_an_unvalidated_warning(self):
        raw = self._scheduler(cache_size=2)
        raw["benchmark"].update(context_tokens=[0], throughput_task="math")
        _, _, prepared = self._preflight(raw)
        self.assertIsNone(prepared["estimate"]["gib"])
        self.assertTrue(any("not validated" in w for w in prepared["warnings"]))

    def test_auto_for_dflash_has_no_qualified_factor(self):
        draft = _write_ir(self.root / "draft", {**DENSE, "dflash_config": {"target_layer_ids": [1],
                                                                           "block_size": 16}})
        raw = self._scheduler(cache_size="auto")
        raw["profiles"][0]["dflash"] = {"model": str(draft), "num_assistant_tokens": 3}
        # The target must carry the hidden-state metadata for DFlash to get this far.
        (self.model_dir / "openvino_model.xml").write_text(
            '<net><layers/><rt_info><hidden_states_decoder_layers value="{}"/></rt_info></net>',
            encoding="utf-8")
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(raw)
        self.assertEqual(caught.exception.category, "cache_unknown")
        self.assertIn("dflash", caught.exception.message)

    def test_missing_target_ir_is_missing_ir(self):
        (self.model_dir / "openvino_tokenizer.xml").unlink()
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(_raw())
        self.assertEqual(caught.exception.category, "missing_ir")
        self.assertIn("never downloads", caught.exception.message)

    def test_dflash_without_target_metadata_is_a_preflight_failure_with_a_hint(self):
        draft = _write_ir(self.root / "draft", {**DENSE, "dflash_config": {"target_layer_ids": [1]}})
        raw = _raw()
        raw["profiles"][0]["dflash"] = {"model": str(draft), "num_assistant_tokens": 3}
        before = (self.model_dir / "openvino_model.xml").read_bytes()
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(raw)
        self.assertEqual(caught.exception.category, "missing_ir")
        self.assertIn("<weight_format>-dflash", caught.exception.message)
        self.assertEqual((self.model_dir / "openvino_model.xml").read_bytes(), before)

    def test_dflash_vocabulary_mismatch_is_rejected(self):
        draft = _write_ir(self.root / "draft", {**DENSE, "vocab_size": 7,
                                                "dflash_config": {"target_layer_ids": [1]}})
        raw = _raw()
        raw["profiles"][0]["dflash"] = {"model": str(draft), "num_assistant_tokens": 3}
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(raw)
        self.assertEqual(caught.exception.category, "artifact")

    def _eagle3_draft(self, **config):
        """A VLM Eagle3 drafter IR as the serving recipe exports it."""
        draft = _write_ir(self.root / "eagle3", {
            "architectures": ["LlamaForCausalLMEagle3"], "target_model_type": "qwen3_vl",
            "num_hidden_layers": 1, "num_key_value_heads": 8, "head_dim": 128, **config})
        ports = "".join(
            f'<layer type="Parameter" name="{name}"><output><port id="0">{"<dim/>" * rank}'
            f'</port></output></layer>'
            for name, rank in (("hidden_states", 3), ("inputs_embeds", 3), ("position_ids", 3)))
        ports += "".join(f'<layer type="Result" name="r{i}" output_names="{name}"/>'
                         for i, name in enumerate(("logits", "d2t")))
        (draft / "openvino_model.xml").write_text(
            f'<net><layers>{ports}</layers><rt_info><eagle3_mode value="True" /></rt_info></net>',
            encoding="utf-8")
        return draft

    def _eagle3_raw(self, draft):
        (self.model_dir / "config.json").write_text(
            json.dumps({**DENSE, "model_type": "qwen3_vl"}), encoding="utf-8")
        raw = _raw()
        raw["profiles"][0]["eagle3"] = {"model": str(draft), "num_assistant_tokens": 4}
        return raw

    def test_eagle3_uses_the_ordinary_target_and_counts_the_draft_kv(self):
        from components.vlm.text_gen_prepare import eagle3_ir_interface

        draft = self._eagle3_draft()
        self.assertEqual(eagle3_ir_interface(draft / "openvino_model.xml")[0]["position_ids"], 3)
        # No hidden-state rt_info on the target: that is a DFlash requirement only.
        _, _, prepared = self._preflight(self._eagle3_raw(draft))
        self.assertEqual(prepared["mtp"]["strategy"], "eagle3")
        self.assertEqual(prepared["draft_dir"], draft)
        self.assertGreater(prepared["estimate"]["speculative_bytes"], 0)

    def test_an_eagle3_drafter_for_another_model_is_rejected(self):
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(self._eagle3_raw(self._eagle3_draft(target_model_type="llama")))
        self.assertEqual(caught.exception.category, "artifact")
        self.assertIn("different model", caught.exception.message)

    def test_a_missing_eagle3_drafter_is_missing_ir(self):
        with self.assertRaises(benchmark._Rejected) as caught:
            self._preflight(self._eagle3_raw(self.root / "absent"))
        self.assertEqual(caught.exception.category, "missing_ir")
        self.assertIn("eagle3", caught.exception.message)


class TestIrReads(unittest.TestCase):
    def test_fixed_state_reads_unique_conv_and_ssm_variables(self):
        xml = ('<layer><data variable_id="cache_params.past.conv.0" variable_type="f32" '
               'variable_shape="?,8,4"/></layer><layer><data variable_id="cache_params.past.ssm.0" '
               'variable_type="f32" variable_shape="?,2,4,4"/></layer><layer><data '
               'variable_id="cache_params.past.conv.0" variable_type="f32" variable_shape="?,8,4"/>'
               '</layer><layer><data variable_id="cache_params.past.key.0" variable_type="f32" '
               'variable_shape="?,4,?,256"/></layer>')
        with tempfile.TemporaryDirectory() as root:
            directory = _write_ir(Path(root, "m"), DENSE, state_xml=xml)
            self.assertEqual(fixed_state_cache_bytes(directory), (8 * 4 + 2 * 4 * 4) * 4)

    def test_missing_ir_state_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(fixed_state_cache_bytes(root))

    def test_weight_precision_comes_from_the_ir_not_the_label(self):
        block = ('<net><layers/><rt_info><nncf><weight_compression><mode value="int4_asym"/>'
                 '<group_size value="64"/><backup_mode value="int8_sym"/><ratio value="1.0"/>'
                 '</weight_compression></nncf></rt_info></net>')
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(ir_weight_precision(root))
            Path(root, "openvino_language_model.xml").write_text(block, encoding="utf-8")
            precision = ir_weight_precision(root)
        self.assertEqual(precision, "int4_asym g64 backup int8_sym")
        self.assertTrue(sizing.precision_matches_label(precision, "int4"))
        self.assertFalse(sizing.precision_matches_label(precision, "int8"))
        self.assertTrue(sizing.precision_matches_label(None, "int4"))

    def test_layout_requires_graph_weight_pairs_and_tokenizer_assets(self):
        with tempfile.TemporaryDirectory() as root:
            directory = _write_ir(Path(root, "vlm"), HYBRID, layout="vlm")
            self.assertEqual(check_ir_layout(directory), [])
            (directory / "openvino_text_embeddings_model.bin").unlink()
            self.assertEqual(check_ir_layout(directory), ["openvino_text_embeddings_model.bin"])
            (directory / "tokenizer_config.json").unlink()
            self.assertIn("tokenizer_config.json", check_ir_layout(directory))
            self.assertEqual(check_ir_layout(Path(root, "absent")), ["<directory>"])


class TestShippedConfigs(unittest.TestCase):
    # One config per model; the weight formats each documents are selected with --weight-format.
    # The first label is the config's default.
    WEIGHTS = {"config_qwen3.6_35b.yaml": ("int4",), "config_qwen3.8_27b.yaml": ("int4", "int8"),
               "config_qwen3.5_9b.yaml": ("int4", "int8"),
               "config_qwen3_8b.yaml": ("int4", "int8")}
    CONFIGS = tuple(WEIGHTS)

    def _load(self, name, *argv):
        path = settings.TOOL_DIR / name
        return settings._load_settings(settings._parse_args(["--config", str(path), *argv]))

    def test_every_config_shares_the_qwen36_core_settings(self):
        """Same workload, repetition, budgets and default weights; only the model, its output
        directory and its profiles differ (DFlash profiles exist only for Qwen3.6)."""
        core = ("weight_format", "device", "context_tokens", "output_tokens", "warmup",
                "iterations", "iteration_gap_sec", "throughput_tasks", "timeout_sec",
                "max_system_memory_pct", "gpu_memory_budget_gb", "cache_dir")
        expected = {k: self._load("config_qwen3.6_35b.yaml")[k] for k in core}
        self.assertEqual(expected["throughput_tasks"], ("code", "math", "chat", "summary_2s"))
        for name, labels in self.WEIGHTS.items():
            with self.subTest(config=name):
                resolved = self._load(name)
                self.assertEqual({k: resolved[k] for k in core}, expected)
                self.assertEqual(resolved["weight_format"], labels[0])

    def test_only_the_documented_configs_ship(self):
        shipped = sorted(p.name for p in settings.TOOL_DIR.glob("config_*.yaml"))
        self.assertEqual(shipped, sorted(self.CONFIGS))

    def test_every_config_resolves_with_accuracy_and_keeps_measurement_validity(self):
        for name in self.CONFIGS:
            with self.subTest(config=name):
                resolved = self._load(name, "--accuracy")
                self.assertEqual((resolved["warmup"], resolved["iterations"]), (1, 3))
                self.assertFalse(resolved["record_text"])
                self.assertIn(resolved["accuracy"]["baseline_profile"],
                              [p["name"] for p in resolved["profiles"]])
                for profile in resolved["profiles"]:
                    if profile["pipeline_mode"] == settings.PIPELINE_PAGED:
                        self.assertEqual(profile["scheduler"]["max_num_seqs"], 1)
                        self.assertGreater(profile["scheduler"]["max_num_batched_tokens"], 256)
                        self.assertIs(profile["scheduler"]["enable_prefix_caching"], False)
                    self.assertIn(profile["ov"].get("KV_CACHE_PRECISION", "f16"),
                                  sizing._KV_PRECISION_BYTES)

    def test_accuracy_sections_match_the_reference_configuration(self):
        expected = self._load("config_qwen3.6_35b.yaml", "--accuracy")["accuracy"]
        for name in self.CONFIGS:
            with self.subTest(config=name):
                self.assertEqual(self._load(name, "--accuracy")["accuracy"], expected)
        self.assertEqual((expected["seed"], expected["baseline_profile"]), (20260917, "paged_min"))
        self.assertEqual(settings.accuracy.planned_probe_count(expected), 32)

    def test_the_weight_format_selects_the_serving_artifact(self):
        for name, labels in self.WEIGHTS.items():
            for label in labels:
                with self.subTest(config=name, weights=label):
                    resolved = self._load(name, "--weight-format", label)
                    for model in resolved["models"]:
                        self.assertEqual(settings.model_ir_dir(resolved, model),
                                         settings.SC_ROOT / "models" / "openvino" / model / label)

    def test_documented_test_steps_match_the_plan(self):
        """Each config's TEST STEPS grids are exactly what the scripts plan, IDs included."""
        for name in self.CONFIGS:
            text = (settings.TOOL_DIR / name).read_text(encoding="utf-8")
            documented = {line.lstrip("#").strip() for line in text.splitlines()
                          if line.startswith("#")}
            for flags in ((), ("--accuracy",)):
                with self.subTest(config=name, flags=flags):
                    planned = settings.plan_run(self._load(name, *flags))
                    for line in settings.plan_grid(planned):
                        self.assertIn(line.strip(), documented)
                    kind = "Accuracy run (--accuracy)" if flags else "Throughput run (default)"
                    self.assertTrue(any(line.startswith(f"{kind}: {len(planned)} case")
                                        for line in documented), kind)

    def test_the_guide_shows_each_default_plan(self):
        guide = (settings.SC_ROOT.parent / "docs" / "dev-guide" / "context-bench" /
                 "context-bench-guide.md").read_text(encoding="utf-8").splitlines()
        for name in self.CONFIGS:
            with self.subTest(config=name):
                for line in settings.plan_grid(settings.plan_run(self._load(name))):
                    self.assertIn(line, guide)

    def test_case_ids_follow_execution_order(self):
        planned = settings.plan_run(self._load("config_qwen3.6_35b.yaml"))
        self.assertEqual([p["case_id"] for p in planned], [f"c{i:02d}" for i in range(1, 36)])
        self.assertEqual(planned[0]["label"],
                         "Qwen3.6-35B-A3B | paged_min | no transcript (task prompt only) | code")
        self.assertEqual((settings.case_id(7, 9), settings.case_id(7, 120)), ("c07", "c007"))

    def test_qwen36_matrix_and_matched_dflash_sweep(self):
        resolved = self._load("config_qwen3.6_35b.yaml")
        profiles = resolved["profiles"]
        baseline = profiles[0]
        self.assertEqual(baseline["name"], "paged_min")
        self.assertFalse(baseline["mtp"]["enabled"])
        self.assertEqual([p["mtp"]["num_assistant_tokens"] for p in profiles[1:]], [3, 5, 7, 15])
        for profile in profiles[1:]:
            self.assertEqual(profile["mtp"]["strategy"], "dflash")
            self.assertEqual(profile["mtp"]["model"], "models/openvino/Qwen3.6-35B-A3B-DFlash/int4-lmhead")
            self.assertEqual(profile["ov"], baseline["ov"])
            self.assertEqual(profile["scheduler"], baseline["scheduler"])
            self.assertGreaterEqual(profile["scheduler"]["num_linear_attention_blocks"],
                                    1 + profile["mtp"]["num_assistant_tokens"] + 1)
        self.assertEqual(resolved["context_tokens"], (0, 8000))
        self.assertEqual(resolved["output_tokens"], 512)
        self.assertEqual(resolved["iteration_gap_sec"], 30.0)
        # The reference's task list: classroom_summary runs on its own with 1024 tokens.
        self.assertEqual(resolved["throughput_tasks"], ("code", "math", "chat", "summary_2s"))
        # The draft follows the target device, so --device overrides every profile at once.
        cpu = self._load("config_qwen3.6_35b.yaml", "--device", "CPU")
        self.assertEqual(cpu["device"], "CPU")

    def test_qwen38_config_is_one_plain_paged_baseline(self):
        for label in self.WEIGHTS["config_qwen3.8_27b.yaml"]:
            with self.subTest(weights=label):
                resolved = self._load("config_qwen3.8_27b.yaml", "--weight-format", label)
                self.assertEqual(resolved["weight_format"], label)
                self.assertEqual([(p["name"], p["pipeline_mode"], p["mtp"]["enabled"])
                                  for p in resolved["profiles"]],
                                 [("paged_min", settings.PIPELINE_PAGED, False)])

    def _assert_matched_sweep(self, resolved, strategy, draft, windows):
        """paged_min, then one speculative profile per window, identical otherwise."""
        profiles = resolved["profiles"]
        baseline = profiles[0]
        self.assertEqual(baseline["name"], "paged_min")
        self.assertFalse(baseline["mtp"]["enabled"])
        self.assertEqual([p["name"] for p in profiles[1:]], [f"{strategy}_k{k}" for k in windows])
        self.assertEqual([p["mtp"]["num_assistant_tokens"] for p in profiles[1:]], list(windows))
        for profile in profiles:
            self.assertEqual(profile["pipeline_mode"], settings.PIPELINE_PAGED)
        for profile in profiles[1:]:
            self.assertEqual(profile["mtp"]["strategy"], strategy)
            self.assertEqual(profile["mtp"]["model"], draft)
            self.assertEqual(profile["ov"], baseline["ov"])
            self.assertEqual(profile["scheduler"], baseline["scheduler"])

    def test_qwen35_matrix_and_matched_dflash_sweep(self):
        resolved = self._load("config_qwen3.5_9b.yaml")
        self._assert_matched_sweep(resolved, "dflash",
                                   "models/openvino/Qwen3.5-9B-DFlash/int4-lmhead", (3, 5, 7, 15))
        for profile in resolved["profiles"][1:]:
            self.assertGreaterEqual(profile["scheduler"]["num_linear_attention_blocks"],
                                    1 + profile["mtp"]["num_assistant_tokens"] + 1)

    def test_qwen3_vl_8b_matched_eagle3_sweep_reads_the_vlm_export(self):
        resolved = self._load("config_qwen3_8b.yaml")
        self.assertEqual(resolved["models"], ("Qwen3-VL-8B-Instruct",))
        self._assert_matched_sweep(resolved, "eagle3",
                                   "models/openvino/Qwen3-VL-8B-Instruct-eagle3/int4", (3, 4, 5))
        # Serving's qualified Eagle3 windows (text_gen_profiles) cover the sweep.
        from components.vlm.text_gen_profiles import target_profile

        low, high = target_profile("Qwen/Qwen3-VL-8B-Instruct").methods["eagle3"].num_assistant_tokens_range
        self.assertTrue(all(low <= p["mtp"]["num_assistant_tokens"] <= high
                            for p in resolved["profiles"][1:]))
        # The dense model has no linear-attention rows to reserve.
        self.assertNotIn("num_linear_attention_blocks", resolved["profiles"][0]["scheduler"])
        # A dense model with its text architecture nested under text_config, as in the export.
        config = {"text_config": {"num_hidden_layers": 36, "num_key_value_heads": 8,
                                  "head_dim": 128}}
        self.assertEqual(sizing.theoretical_kv_bytes_per_token(config), 36 * 2 * 8 * 128 * 2)

    def test_profiles_that_resolve_identically_are_refused(self):
        with self.assertRaisesRegex(settings.ConfigError, "resolve to the same configuration"):
            self._load("config_qwen3.6_35b.yaml", "--assistant-tokens", "5")
        one = self._load("config_qwen3.6_35b.yaml", "--assistant-tokens", "5",
                         "--profiles", "paged_min", "dflash_k7")
        self.assertEqual([p["mtp"]["num_assistant_tokens"] for p in one["profiles"]], [None, 5])

    def test_unlisted_models_use_the_documented_fallback_derivation(self):
        resolved = _resolve(_raw(models=["Qwen/Qwen3.5-9B"]))
        self.assertEqual(settings.model_ir_dir(resolved, "Qwen/Qwen3.5-9B"),
                         settings.SC_ROOT / "models" / "openvino" / "Qwen3.5-9B" / "int4")


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""``models.text_gen.speculative`` resolution against the serving catalog."""

from types import SimpleNamespace

import pytest

from components.vlm.text_gen_profiles import (
    SpeculativeConfigError,
    SpeculativeProfile,
    SpeculativeUnavailableError,
    TargetProfile,
    _auto_method,
    preconverted_repo,
    resolve_plan,
    version_at_least,
)
from utils.model_paths import OPENVINO_MODELS_DIR, SC_ROOT

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _text_gen(name="Qwen/Qwen3.6-35B-A3B", weight="int4", speculative="__absent__"):
    node = {"provider": "vlm", "vlm_name": name, "device": "GPU", "weight_format": weight,
            "max_new_tokens": 5120}
    if speculative != "__absent__":
        node["speculative"] = speculative
    return SimpleNamespace(**node)


# --- backward compatibility ------------------------------------------------
def test_absent_block_is_plain_and_keeps_the_target_path():
    plan = resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct"))
    assert plan.mode == "off"
    assert plan.speculative is None and not plan.wants_speculative
    assert plan.target_dir == OPENVINO_MODELS_DIR / "Qwen3-VL-8B-Instruct" / "int4"


def test_unknown_older_model_stays_plain_without_catalog_entry():
    plan = resolve_plan(_text_gen("Qwen/Qwen2.5-VL-7B-Instruct"))
    assert plan.target is None and plan.speculative is None
    assert plan.tool_calls_declared is None


def test_off_ignores_dormant_fields_even_if_invalid():
    plan = resolve_plan(_text_gen(speculative={"mode": "off", "method": "bogus", "x": 1}))
    assert plan.mode == "off" and plan.speculative is None


def test_yaml_boolean_off_is_off():
    assert resolve_plan(_text_gen(speculative={"mode": False})).mode == "off"


def test_null_block_is_plain():
    assert resolve_plan(_text_gen(speculative=None)).mode == "off"


def test_namespace_block_from_config_loader_is_accepted():
    plan = resolve_plan(_text_gen(speculative=SimpleNamespace(mode="auto")))
    assert plan.speculative.method == "dflash"


# --- defaults from the catalog ---------------------------------------------
def test_auto_method_selects_catalog_dflash_for_qwen36():
    plan = resolve_plan(_text_gen(speculative={"mode": "auto"}))
    spec = plan.speculative
    assert spec.method == "dflash"
    assert spec.source == "huggingface"
    assert spec.draft_model == "z-lab/Qwen3.6-35B-A3B-DFlash"
    assert spec.num_assistant_tokens == 5
    assert spec.draft_dir() == OPENVINO_MODELS_DIR / "Qwen3.6-35B-A3B-DFlash" / "int4"
    assert spec.profile.scheduler["enable_prefix_caching"] is False


def test_auto_method_selects_catalog_eagle3_for_qwen3_vl_8b():
    plan = resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct", "int4", {"mode": "auto"}))
    spec = plan.speculative
    assert spec.method == "eagle3" and spec.source == "huggingface"
    assert spec.draft_model == "AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3"
    assert spec.draft_revision and len(spec.draft_revision) == 40  # pinned commit
    assert spec.num_assistant_tokens == 4
    assert spec.draft_dir() == OPENVINO_MODELS_DIR / "Qwen3-VL-8B-Instruct-eagle3" / "int4"
    assert spec.profile.min_genai_version == "2026.4"
    # LOW queue priority erases the speedup; the profile pins MEDIUM.
    assert spec.profile.ov_properties["GPU_QUEUE_PRIORITY"] == "MEDIUM"
    # f16 KV slowed long classroom prompts; the runtime default is kept.
    assert "KV_CACHE_PRECISION" not in spec.profile.ov_properties


@pytest.mark.parametrize("weight, k", [("int4", 5), ("int8", 7)])
def test_auto_method_selects_dflash_for_qwen35(weight, k):
    plan = resolve_plan(_text_gen("Qwen/Qwen3.5-9B", weight, {"mode": "auto"}))
    assert plan.speculative.method == "dflash"
    assert plan.speculative.draft_model == "z-lab/Qwen3.5-9B-DFlash"
    # Per-precision default window; an explicit setting still wins.
    assert plan.speculative.num_assistant_tokens == k
    plan = resolve_plan(_text_gen("Qwen/Qwen3.5-9B", weight, {
        "mode": "auto", "num_assistant_tokens": 3}))
    assert plan.speculative.num_assistant_tokens == 3


def test_auto_method_falls_back_to_a_method_covering_the_precision():
    target = TargetProfile(name="m", ir_repos={}, default_method="dflash", methods={
        "dflash": SpeculativeProfile(method="dflash", precisions=("int4",)),
        "mtp": SpeculativeProfile(method="mtp", default_source="target")})
    assert _auto_method(target, "int4", None) == "dflash"
    assert _auto_method(target, "int8", None) == "mtp"
    assert _auto_method(target, "int4", "target") == "mtp"
    assert _auto_method(target, "fp16", None) == "dflash"  # reported as unqualified


@pytest.mark.parametrize("weight, block", [
    ("int4", {"mode": "auto", "draft_source": "target"}),  # only MTP uses the target's head
    ("int8", {"mode": "auto", "draft_source": "target"}),
    ("int4", {"mode": "auto", "method": "mtp"}),
    ("int8", {"mode": "auto", "method": "mtp"}),
])
def test_target_bundled_mtp_for_qwen35(weight, block):
    plan = resolve_plan(_text_gen("Qwen/Qwen3.5-9B", weight, block))
    spec = plan.speculative
    assert spec.method == "mtp"
    assert spec.source == "target" and spec.draft_model is None
    assert spec.draft_dir() is None
    assert spec.describe() == "target-bundled MTP"
    assert spec.num_assistant_tokens == 2
    assert spec.profile.min_genai_version == "2026.4.1"
    assert spec.profile.scheduler["max_num_seqs"] == 1


def test_eagle3_rejects_another_drafter_and_out_of_range_windows():
    with pytest.raises(SpeculativeConfigError, match="not the qualified eagle3"):
        resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct", "int4", {
            "mode": "auto", "method": "eagle3", "draft_model": "someone/other-eagle3"}))
    with pytest.raises(SpeculativeConfigError, match="1..8"):
        resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct", "int4", {
            "mode": "auto", "num_assistant_tokens": 9}))


def test_eagle3_is_qualified_for_english_only():
    node = _text_gen("Qwen/Qwen3-VL-8B-Instruct", "int4", {"mode": "auto"})
    assert resolve_plan(node, "en").speculative.method == "eagle3"
    assert resolve_plan(node, "EN ").speculative.method == "eagle3"
    plan = resolve_plan(node, "zh")
    # Plain pipeline, no drafter prepared, the reason reported in /health.
    assert plan.speculative is None and not plan.wants_speculative
    assert "app.language zh" in plan.plain_reason
    with pytest.raises(SpeculativeUnavailableError, match="app.language zh"):
        resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct", "int4", {"mode": "on"}), "zh")
    # DFlash has no language restriction.
    assert resolve_plan(_text_gen(speculative={"mode": "auto"}), "zh").speculative.method == "dflash"


def test_removed_cjk_setting_is_an_unknown_key():
    with pytest.raises(SpeculativeConfigError, match="max_cjk_ratio"):
        resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct", "int4", {
            "mode": "auto", "max_cjk_ratio": 0.1}))


def test_eagle3_int8_is_plain_in_auto_and_fails_in_on():
    plan = resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct", "int8", {"mode": "auto"}))
    assert plan.speculative is None and "int8" in plan.plain_reason
    with pytest.raises(SpeculativeUnavailableError):
        resolve_plan(_text_gen("Qwen/Qwen3-VL-8B-Instruct", "int8", {"mode": "on"}))


def test_eagle3_is_not_offered_for_other_targets():
    plan = resolve_plan(_text_gen(speculative={"mode": "auto", "method": "eagle3"}))
    assert plan.speculative is None and "no qualified eagle3" in plan.plain_reason


@pytest.mark.parametrize("method", ["auto", "dflash2", "mtp"])
def test_unsupported_methods_are_plain_in_auto_and_fail_in_on(method):
    plan = resolve_plan(_text_gen("Qwen/Qwen3.8-27B", "int8", {"mode": "auto", "method": method}))
    assert plan.speculative is None and plan.plain_reason
    with pytest.raises(SpeculativeUnavailableError, match="mode: auto"):
        resolve_plan(_text_gen("Qwen/Qwen3.8-27B", "int4", {"mode": "on", "method": method}))


def test_qwen35_explicit_dflash_selects_the_z_lab_drafter():
    plan = resolve_plan(_text_gen(
        "Qwen/Qwen3.5-9B", "int4", {"mode": "on", "method": "dflash"}))
    spec = plan.speculative
    assert spec.method == "dflash" and spec.source == "huggingface"
    assert spec.draft_model == "z-lab/Qwen3.5-9B-DFlash"
    assert spec.draft_revision and len(spec.draft_revision) == 40  # pinned commit
    assert spec.draft_dir() == OPENVINO_MODELS_DIR / "Qwen3.5-9B-DFlash" / "int4"
    assert spec.profile.min_genai_version == "2026.4.1"
    assert spec.profile.ov_properties["GPU_QUEUE_PRIORITY"] == "MEDIUM"
    # Hybrid target: one linear-attention row per sequence and draft position.
    assert spec.profile.scheduler["num_linear_attention_blocks"] == 17
    assert spec.profile.scheduler["max_num_seqs"] == 1


def test_qwen35_dflash_rejects_the_qwen36_drafter():
    with pytest.raises(SpeculativeConfigError, match="not the qualified dflash"):
        resolve_plan(_text_gen("Qwen/Qwen3.5-9B", "int4", {
            "mode": "auto", "method": "dflash", "draft_model": "z-lab/Qwen3.6-35B-A3B-DFlash"}))


def test_unqualified_precision_falls_back_in_auto():
    plan = resolve_plan(_text_gen("Qwen/Qwen3.6-35B-A3B", "int8", {"mode": "auto"}))
    assert plan.speculative is None and "int8" in plan.plain_reason


def test_explicit_revision_and_tuning_are_kept():
    plan = resolve_plan(_text_gen(speculative={
        "mode": "on", "method": "dflash", "draft_revision": COMMIT, "num_assistant_tokens": 7}))
    assert plan.speculative.draft_revision == COMMIT
    assert plan.speculative.num_assistant_tokens == 7
    assert plan.speculative.describe() == f"z-lab/Qwen3.6-35B-A3B-DFlash@{COMMIT[:12]}"


def test_uppercase_revision_is_normalized_for_preparation():
    plan = resolve_plan(_text_gen(speculative={"mode": "auto", "draft_revision": COMMIT.upper()}))
    assert plan.speculative.draft_revision == COMMIT


def test_local_relative_dir_resolves_from_smart_classroom_root():
    plan = resolve_plan(_text_gen(speculative={
        "mode": "auto", "draft_source": "local", "draft_model": "models/my-draft"}))
    assert plan.speculative.draft_dir() == SC_ROOT / "models" / "my-draft"
    assert plan.speculative.describe() == "local IR (my-draft)"


# --- validation failures (never fallback) -----------------------------------
@pytest.mark.parametrize("block, message", [
    ({"mode": "sometimes"}, "speculative.mode"),
    ({"mode": "auto", "method": "eagle"}, "speculative.method"),
    ({"mode": "auto", "draft_source": "s3"}, "speculative.draft_source"),
    ({"mode": "auto", "unknown": 1}, "unknown speculative key"),
    ({"mode": "auto", "num_assistant_tokens": 0}, "positive integer"),
    ({"mode": "auto", "num_assistant_tokens": True}, "positive integer"),
    ({"mode": "auto", "num_assistant_tokens": 16}, "supported range"),
    ({"mode": "auto", "draft_revision": "main"}, "40-character commit"),
    ({"mode": "auto", "draft_model": "z-lab/Qwen3.8-27B-DFlash2"}, "not the qualified"),
    ({"mode": "auto", "draft_source": "local"}, "requires draft_model"),
    ({"mode": "auto", "draft_source": "local", "draft_model": "d", "draft_revision": COMMIT},
     "must be null"),
    ({"mode": "auto", "method": "dflash", "draft_source": "target"}, "only with method: mtp"),
])
def test_invalid_settings_raise(block, message):
    with pytest.raises(SpeculativeConfigError, match=message):
        resolve_plan(_text_gen(speculative=block))


@pytest.mark.parametrize("block, message", [
    ({"mode": "auto", "method": "mtp", "draft_source": "huggingface"}, "bundled prediction head"),
    ({"mode": "auto", "method": "mtp", "draft_model": "z-lab/x"}, "draft_model must be null"),
    ({"mode": "auto", "method": "mtp", "draft_source": "target", "draft_revision": COMMIT},
     "must be null"),
])
def test_invalid_mtp_settings_raise(block, message):
    with pytest.raises(SpeculativeConfigError, match=message):
        resolve_plan(_text_gen("Qwen/Qwen3.8-27B", "int4", block))


def test_catalog_lists_the_new_target_repos():
    assert preconverted_repo("Qwen/Qwen3.8-27B", "int8") == "OpenVINO/Qwen3.8-27B-int8-ov"
    assert preconverted_repo("qwen/qwen3.6-35b-a3b", "INT4") == "OpenVINO/Qwen3.6-35B-A3B-int4-ov"
    assert preconverted_repo("Qwen/Qwen3.6-35B-A3B", "int8") is None


@pytest.mark.parametrize("installed, required, ok", [
    ("2026.3.0.0", "2026.3", True),
    ("2026.5.0.0.dev20260901", "2026.5", True),
    ("2026.3.0.0", "2026.5", False),
    (None, "2026.3", False),
])
def test_version_at_least(installed, required, ok):
    assert version_at_least(installed, required) is ok

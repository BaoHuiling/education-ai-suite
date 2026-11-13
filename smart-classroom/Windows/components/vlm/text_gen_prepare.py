# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Prepare the ``text_gen`` target and speculative artifacts before serving.

The model owner calls :func:`prepare` when it loads the warm VLM, so every
startup path (the desktop app, ``python main.py``)
prepares the same way: the first start downloads or converts what is missing,
later starts only validate. Request handlers never download or convert.

Order of work (see the serving design, "Model Preparation"):

1. **Select** -- :func:`components.vlm.text_gen_profiles.resolve_plan`.
2. **Prepare** -- the target through the existing download/convert path; a Hub
   drafter at an immutable commit into a staging directory; an explicit local
   drafter is validated, never replaced.
3. **Validate** -- required files, drafter metadata, vocabulary and target depth.
   Accelerated exports that need different target IR get a *derived* directory;
   the ordinary target is never modified.
4. Record a manifest. Activation (load + warm-up) belongs to the model owner.

Acquisition failures (:class:`PreparationError`) let ``mode: auto`` fall back to
plain generation; incompatibility (:class:`CompatibilityError`) always fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from components.vlm.text_gen_profiles import (
    COMMIT_RE as _COMMIT_RE,
    DFLASH_TARGET_RT_INFO_KEY,
    SpeculativeSettings,
    SpeculativeUnavailableError,
    TextGenPlan,
    preconverted_repo,
    version_at_least,
)
from utils.model_paths import OPENVINO_MODELS_DIR, SC_ROOT

logger = logging.getLogger(__name__)

_CONTENT_SEARCH_DIR = SC_ROOT / "content_search"
_UD = os.environ.get("SMART_CLASSROOM_USERDATA")
HF_SOURCES_DIR = (Path(_UD) / "models" / "hf") if _UD and _UD.strip() else (SC_ROOT / "models" / "hf")
MANIFEST_DIR = OPENVINO_MODELS_DIR / ".manifests"

DOWNLOAD_ATTEMPTS = 3
LOCK_TIMEOUT_S = 3600.0
# Weights plus conversion intermediates; exports briefly hold source and IR.
EXPORT_DISK_FACTOR = 2.2

_LANGUAGE_MODEL_XML = ("openvino_language_model.xml", "openvino_model.xml")
_DRAFT_IR_FILES = ("openvino_model.xml", "openvino_model.bin", "config.json")
# Drafter checkpoint files fetched from the Hub: configuration and weights only,
# never repository code.
_DRAFT_SOURCE_PATTERNS = ["*.json", "*.safetensors"]

# optimum-cli exports a quantized model to a temporary directory and moves the
# files to the output. tempfile.mkdtemp() creates that directory with mode 0o700,
# which on Windows (Python >= 3.12.4) is a private ACL (SYSTEM, Administrators,
# owner) that a same-volume move keeps. Exported from an elevated session the
# owner is the Administrators group, so a later non-elevated start could not read
# the drafter. Both export scripts therefore create it with a plain mkdir beside
# the output (sys.argv[-1]): its files inherit the models directory's permissions
# and are renamed, never copied, on the same volume.
_EXPORT_TMP_SUFFIX = ".tmp-"
_EXPORT_TMP_PATCH = f"""\
import os, uuid
import optimum.intel.openvino.utils as _ov_utils
_export_out = os.path.abspath(sys.argv[-1])
def _mkdtemp(suffix=None, prefix=None, dir=None):
    path = _export_out + "{_EXPORT_TMP_SUFFIX}" + uuid.uuid4().hex[:8]
    os.mkdir(path)
    return path
_ov_utils.mkdtemp = _mkdtemp
"""

# optimum-intel ships DFlash drafter classes but selects them only through
# trust_remote_code; the export subprocess registers them instead, so no
# repository code can run. They are subclassed because optimum-intel treats
# classes defined under ``optimum.*`` as wrappers around another model. The
# first lines are convert_worker.skip_unloadable_optional_modules(), inlined
# because this script runs isolated (-I): optional ``datasets`` imports pyarrow,
# whose DLLs Windows Application Control may block, so a module that cannot be
# loaded is marked missing instead of failing the export.
_DFLASH_EXPORT = """\
import importlib, sys
try:
    importlib.import_module("datasets")
except ImportError as exc:
    sys.modules["datasets"] = None
    print(f"Optional module datasets cannot be loaded ({exc}); exporting without it.")
from transformers import AutoModel, AutoModelForCausalLM, Qwen3Config
from optimum.exporters.openvino import model_patcher as mp
class DFlashDraftModel(mp.Qwen3DFlashDraftModel): pass
class DFlashForCausalLM(mp.Qwen3DFlashForCausalLM): pass
AutoModel.register(Qwen3Config, DFlashDraftModel, exist_ok=True)
AutoModelForCausalLM.register(Qwen3Config, DFlashForCausalLM, exist_ok=True)
""" + _EXPORT_TMP_PATCH + """\
from optimum.commands.optimum_cli import main
sys.argv = ["optimum-cli"] + sys.argv[1:]
main()
"""

# optimum-intel 2.2.0 exports a VLM Eagle3 drafter (inputs_embeds, 3D M-RoPE
# position_ids, the target's hidden states) only under transformers <= 4.57.6, so
# the export runs on an export-only overlay put first on sys.path here (-I ignores
# PYTHONPATH). SpecForge checkpoints name the drafter's weights without the
# ``model.`` prefix optimum's LlamaEagle3ForCausalLM expects; a re-keyed copy is
# written beside the output and removed afterwards, the pinned source stays as
# downloaded. The classes are registered for LlamaConfig as subclasses for the
# same reasons as in _DFLASH_EXPORT: no trust_remote_code, and a class defined
# under ``optimum.*`` would be taken for a wrapper of another model.
_EAGLE3_EXPORT = """\
import importlib, json, shutil, sys
from pathlib import Path
overlay, source, weight_format, out = sys.argv[1:5]
sys.path.insert(0, overlay)
try:
    importlib.import_module("datasets")
except ImportError as exc:
    sys.modules["datasets"] = None
    print(f"Optional module datasets cannot be loaded ({exc}); exporting without it.")
from safetensors.torch import load_file, save_file
source, keyed = Path(source), Path(out + ".src")
shutil.rmtree(keyed, ignore_errors=True)
keyed.mkdir(parents=True)
rekey = lambda name: name if name.startswith("model.") else "model." + name
for path in source.glob("*.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data.get("weight_map"), dict):
        data["weight_map"] = {rekey(k): v for k, v in data["weight_map"].items()}
    (keyed / path.name).write_text(json.dumps(data), encoding="utf-8")
for path in source.glob("*.safetensors"):
    tensors = load_file(str(path))
    save_file({rekey(k): v.contiguous() for k, v in tensors.items()}, str(keyed / path.name),
              metadata={"format": "pt"})
from transformers import AutoModel, AutoModelForCausalLM, LlamaConfig
from optimum.exporters.openvino import model_patcher as mp
class Eagle3Model(mp.LlamaEagle3Model): pass
class Eagle3ForCausalLM(mp.LlamaEagle3ForCausalLM): pass
AutoModel.register(LlamaConfig, Eagle3Model, exist_ok=True)
AutoModelForCausalLM.register(LlamaConfig, Eagle3ForCausalLM, exist_ok=True)
""" + _EXPORT_TMP_PATCH + """\
from optimum.commands.optimum_cli import main
sys.argv = ["optimum-cli", "export", "openvino", "--model", str(keyed),
            "--task", "text-generation-with-past", "--library", "transformers",
            "--weight-format", weight_format, "--group-size", "128", "--ratio", "1.0", out]
try:
    main()
finally:
    shutil.rmtree(keyed, ignore_errors=True)
"""
# transformers build (and the hub client it needs) for the Eagle3 export overlay.
_EAGLE3_EXPORT_TRANSFORMERS = "4.57.6"
_EAGLE3_EXPORT_EXTRA = ("huggingface_hub==0.36.0",)
# Drafter IR interface the GenAI Eagle3 runtime drives for a VLM target.
_EAGLE3_INPUTS = ("hidden_states", "inputs_embeds", "position_ids")
_EAGLE3_OUTPUTS = ("logits", "d2t")
EAGLE3_DRAFT_RT_INFO_KEY = "eagle3_mode"


class PreparationError(RuntimeError):
    """An artifact could not be acquired or produced (network, disk, export)."""


class CompatibilityError(PreparationError):
    """Artifacts exist but contradict each other or the selected profile."""


@dataclass
class PreparedSpeculative:
    settings: SpeculativeSettings
    target_dir: Path              # derived DFlash target, or the ordinary one
    draft_dir: Path               # external drafter IR or target directory for bundled MTP
    draft_revision: Optional[str]


@dataclass
class PreparedArtifacts:
    plan: TextGenPlan
    target_dir: Path
    speculative: Optional[PreparedSpeculative] = None
    # Sanitized reason the requested acceleration is not available.
    speculative_error: Optional[str] = None
    timings: dict = field(default_factory=dict)
    manifest: Optional[Path] = None


def _installed_version(distribution: str) -> Optional[str]:
    try:
        from importlib.metadata import version

        return version(distribution)
    except Exception:  # noqa: BLE001 - not installed / unreadable metadata
        return None


def installed_genai_version() -> Optional[str]:
    return _installed_version("openvino-genai")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def prepare(plan: TextGenPlan, *,
            genai_version: Optional[Callable[[], Optional[str]]] = None) -> PreparedArtifacts:
    """Prepare the plan's artifacts; raises on target failure.

    A speculative acquisition failure is an error under ``mode: "on"``;
    otherwise it is reported in ``speculative_error`` for plain fallback.
    """
    timings: dict = {}
    t0 = time.monotonic()
    target_dir = _prepare_target(plan)
    timings["target_s"] = round(time.monotonic() - t0, 3)

    result = PreparedArtifacts(plan=plan, target_dir=target_dir, timings=timings)
    strict = plan.mode == "on"
    if plan.wants_speculative:
        t1 = time.monotonic()
        try:
            _check_runtime(plan.speculative, (genai_version or installed_genai_version)())
            result.speculative = _prepare_speculative(plan, target_dir)
        except CompatibilityError:
            raise
        except (PreparationError, SpeculativeUnavailableError, OSError, ET.ParseError) as exc:
            # OSError: a drafter file held open by another process, a full disk.
            if strict:
                raise
            result.speculative_error = str(exc)
            logger.warning("Speculative artifacts unavailable; plain generation will be "
                           "used: %s", exc)
        timings["speculative_s"] = round(time.monotonic() - t1, 3)
    elif plan.plain_reason:
        result.speculative_error = plan.plain_reason

    result.manifest = _write_manifest(result)
    return result


def _check_runtime(settings: SpeculativeSettings, installed: Optional[str]) -> None:
    need = settings.profile.min_genai_version
    if not version_at_least(installed, need):
        raise SpeculativeUnavailableError(
            f"{settings.method} needs openvino-genai >= {need} "
            f"(installed: {installed or 'unknown'})"
        )


# ----------------------------------------------------------------------------
# Target
# ----------------------------------------------------------------------------
def _convert_helpers():
    if str(_CONTENT_SEARCH_DIR) not in sys.path:
        sys.path.append(str(_CONTENT_SEARCH_DIR))
    from components.vlm.vlm_openvino_serving.utils.utils import convert_model, is_model_ready

    return convert_model, is_model_ready


def _prepare_target(plan: TextGenPlan) -> Path:
    model_dir = plan.target_dir
    convert_model, is_model_ready = _convert_helpers()
    if not is_model_ready(model_dir, require_detokenizer=True):
        model_dir.mkdir(parents=True, exist_ok=True)
        with ArtifactLock(model_dir):
            if not is_model_ready(model_dir, require_detokenizer=True):
                logger.info("Preparing text_gen target %s (%s) at %s",
                            plan.model_name, plan.weight_format, _rel(model_dir))
                def convert():
                    convert_model(plan.model_name, str(model_dir), model_type="vlm",
                                  weight_format=plan.weight_format)

                try:
                    if preconverted_repo(plan.model_name, plan.weight_format):
                        # A resumable download: transient network errors are retried.
                        _with_retries(convert, f"{plan.model_name} {plan.weight_format} IR")
                    else:
                        convert()  # a local export; a failure would only repeat
                except Exception as exc:  # noqa: BLE001 - surfaced with context
                    raise PreparationError(
                        f"text_gen target {plan.model_name} could not be prepared: {exc}"
                    ) from exc
        if not is_model_ready(model_dir, require_detokenizer=True):
            raise PreparationError(
                f"text_gen target {plan.model_name} is incomplete after preparation"
            )
    check_tokenizer_runtime(model_dir, _installed_version("openvino-tokenizers"))
    return model_dir


_TOKENIZERS_VERSION_RE = re.compile(rb'<openvino_tokenizers_version value="([^"]+)"')


def check_tokenizer_runtime(model_dir: Path, installed: Optional[str]) -> None:
    """Refuse a tokenizer IR exported by a newer openvino-tokenizers release.

    An older runtime crashes natively loading it (no Python exception), so the
    backend would otherwise exit silently while loading the pipeline.
    """
    try:
        found = _TOKENIZERS_VERSION_RE.search((model_dir / "openvino_tokenizer.xml").read_bytes())
    except OSError:
        return
    if not found or not installed:
        return
    exported = found.group(1).decode("ascii", "replace")
    release = ".".join(exported.split(".")[:2])
    if not version_at_least(installed, release):
        raise PreparationError(
            f"{_rel(model_dir)} was exported with openvino-tokenizers {release}, newer than "
            f"the installed {installed}; upgrade the backend venv "
            "(pip install -r requirements.txt) before loading this model"
        )


# ----------------------------------------------------------------------------
# Speculative artifacts
# ----------------------------------------------------------------------------
def _prepare_speculative(plan: TextGenPlan, target_dir: Path) -> PreparedSpeculative:
    settings = plan.speculative
    if settings.method == "mtp":
        mtp_files = ("openvino_mtp_model.xml", "openvino_mtp_model.bin")
        _require_files(target_dir, mtp_files, "target-bundled MTP drafter IR")
        if any((target_dir / name).is_symlink() for name in mtp_files):
            raise CompatibilityError("target-bundled MTP artifacts must not be symlinks")
        try:
            ET.parse(str(target_dir / "openvino_mtp_model.xml"))
        except ET.ParseError as exc:
            raise CompatibilityError(
                f"target-bundled MTP IR in {_rel(target_dir)} is invalid"
            ) from exc
        # The runtime reads the bundled head from the ordinary target directory.
        return PreparedSpeculative(settings, target_dir, target_dir, None)
    if settings.method == "dflash":
        draft_dir, revision = _prepare_external_draft(plan)
        validate_dflash_pair(target_dir, draft_dir, settings.num_assistant_tokens)
        derived = ensure_dflash_target(target_dir, draft_dir)
        if settings.source != "local":  # a local drafter is used exactly as given
            draft_dir = ensure_draft_head(target_dir, draft_dir, plan.weight_format)
        return PreparedSpeculative(settings, derived, draft_dir, revision)
    if settings.method == "eagle3":
        # The runtime taps the target's hidden states itself: the ordinary
        # target is used as-is, and the drafter keeps its own draft-vocab head.
        draft_dir, revision = _prepare_external_draft(plan)
        validate_eagle3_pair(target_dir, draft_dir)
        return PreparedSpeculative(settings, target_dir, draft_dir, revision)
    # Only reachable if the catalog grows a method without a preparation adapter.
    raise SpeculativeUnavailableError(f"no preparation adapter for {settings.method}")


def _prepare_external_draft(plan: TextGenPlan) -> tuple:
    settings = plan.speculative
    draft_dir = settings.draft_dir()
    if settings.source == "local":
        if not draft_dir.is_dir():
            raise PreparationError(
                f"speculative.draft_model directory {settings.describe()} does not exist; "
                "local drafters are validated, never downloaded"
            )
        _require_files(draft_dir, _DRAFT_IR_FILES, "local drafter IR")
        unreadable = _unreadable_files(draft_dir, _DRAFT_IR_FILES)
        if unreadable:
            raise PreparationError(
                f"local drafter IR {settings.describe()} is not readable by this user "
                f"({', '.join(unreadable)}); grant read access to its files"
            )
        return draft_dir, None

    repo = settings.draft_model
    pinned = settings.draft_revision or _recorded_draft_revision(plan, repo)
    revision = _ready_revision(draft_dir, pinned)
    if revision:
        return draft_dir, revision

    draft_dir.parent.mkdir(parents=True, exist_ok=True)
    with ArtifactLock(draft_dir):
        revision = _ready_revision(draft_dir, pinned)  # another start may have built it
        if revision:
            return draft_dir, revision
        revision = pinned or _with_retries(lambda: _resolve_revision(repo), f"revision of {repo}")
        source_dir = _download_source(repo, revision)
        # Replaces (sets aside) an incomplete export or one of another revision.
        _export_draft(source_dir, draft_dir, settings, repo, revision)
    return draft_dir, revision


def _ready_revision(draft_dir: Path, pinned: Optional[str]) -> Optional[str]:
    """Revision of a complete, readable drafter export matching ``pinned`` (any if None)."""
    if not all((draft_dir / f).is_file() for f in _DRAFT_IR_FILES):
        return None
    if _unreadable_files(draft_dir, _DRAFT_IR_FILES + (".prepared.json",)):
        return None  # re-exported; _export_draft sets the unreadable one aside
    revision = _read_json(draft_dir / ".prepared.json").get("revision")
    return revision if revision and pinned in (None, revision) else None


def _download_source(repo: str, revision: str) -> Path:
    if not _COMMIT_RE.match(str(revision)):
        raise PreparationError(f"refusing non-commit revision for {repo}")
    final = HF_SOURCES_DIR / repo.replace("/", "__") / revision
    if (final / "config.json").is_file():
        return final
    staging = final.with_name(final.name + ".partial")
    staging.mkdir(parents=True, exist_ok=True)
    _check_disk(staging, _with_retries(lambda: _repo_size(repo, revision),
                                       f"size of {repo}") * EXPORT_DISK_FACTOR)
    logger.info("Downloading drafter source %s@%s", repo, revision[:12])
    # Resumable: snapshot_download skips files already complete in local_dir.
    _with_retries(lambda: _snapshot(repo, staging, revision, _DRAFT_SOURCE_PATTERNS),
                  f"{repo}@{revision[:12]}")
    if not (staging / "config.json").is_file():
        raise PreparationError(f"{repo}@{revision[:12]} has no config.json")
    staging.replace(final)
    return final


def _export_draft(source_dir: Path, draft_dir: Path, settings: SpeculativeSettings,
                  repo: str, revision: str) -> None:
    """Convert a drafter checkpoint to IR through the profile's recipe.

    No ``--trust-remote-code``: repository code is never executed (see
    ``_DFLASH_EXPORT`` and ``_EAGLE3_EXPORT``). A checkpoint the installed
    exporter cannot convert fails here, which ``auto`` reports as a plain fallback.
    """
    staging = draft_dir.with_name(draft_dir.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    for leftover in staging.parent.glob(staging.name + _EXPORT_TMP_SUFFIX + "*"):
        shutil.rmtree(leftover, ignore_errors=True)  # an interrupted export's temp dir
    fmt = settings.profile.draft_export_weight_format or "int4"
    # The interpreter's own environment; -X utf8 keeps the exporter's progress
    # output printable on non-UTF-8 consoles.
    if settings.method == "eagle3":
        cmd = [sys.executable, "-I", "-X", "utf8", "-c", _EAGLE3_EXPORT,
               str(_eagle3_overlay()), str(source_dir), fmt, str(staging)]
        needs = (f"optimum-intel>=2.2.0 and transformers {_EAGLE3_EXPORT_TRANSFORMERS} in the "
                 "export overlay")
    else:
        # --all-layers as the reference DFlash drafter.
        cmd = [sys.executable, "-I", "-X", "utf8", "-c", _DFLASH_EXPORT,
               "export", "openvino", "--model", str(source_dir),
               "--task", "text-generation-with-past", "--weight-format", fmt, "--all-layers",
               str(staging)]
        needs = "optimum-intel>=2.2.0"
    env = os.environ.copy()
    env["HF_HUB_OFFLINE"] = "1"   # convert only what was downloaded and pinned
    logger.info("Exporting %s drafter %s to OpenVINO IR (%s)", settings.method, repo, fmt)
    completed = subprocess.run(cmd, env=env, check=False)
    if completed.returncode != 0:
        raise PreparationError(
            f"drafter export of {repo} failed (exit {completed.returncode}); it needs "
            f"{needs} (pip install -r requirements.txt)"
        )
    source_cfg = _read_json(source_dir / "config.json")
    exported_cfg = _read_json(staging / "config.json")
    if "dflash_config" in source_cfg and "dflash_config" not in exported_cfg:
        # The drafter metadata the runtime and validation read; copied into
        # our own derived directory, never into the source snapshot.
        exported_cfg["dflash_config"] = source_cfg["dflash_config"]
        _write_json(staging / "config.json", exported_cfg)
    _require_files(staging, _DRAFT_IR_FILES, f"exported drafter {repo}")
    _write_json(staging / ".prepared.json", {
        "repo": repo,
        "revision": revision,
        "weight_format": fmt,
        "exported_at": _now(),
        "files": _file_sizes(staging),
    })
    if draft_dir.exists():
        unreadable = _unreadable_files(draft_dir, _DRAFT_IR_FILES)
        if unreadable:
            logger.warning(
                "Drafter export %s is not readable by this user (%s), typically because it "
                "was exported from an elevated session; replacing it",
                _rel(draft_dir), ", ".join(unreadable))
        _set_aside(draft_dir, "unreadable" if unreadable else "superseded")
    staging.replace(draft_dir)


def _unreadable_files(directory: Path, names) -> list:
    """Names in ``directory`` that exist but this process may not open for reading."""
    blocked = []
    for name in names:
        try:
            with open(directory / name, "rb"):
                pass
        except FileNotFoundError:
            continue
        except PermissionError:
            blocked.append(name)
    return blocked


def _eagle3_overlay() -> Path:
    """Export-only transformers build for the Eagle3 drafter, provisioned once."""
    if str(_CONTENT_SEARCH_DIR) not in sys.path:
        sys.path.append(str(_CONTENT_SEARCH_DIR))
    from components.vlm.vlm_openvino_serving.utils.utils import ensure_transformers_overlay

    try:
        return ensure_transformers_overlay(_EAGLE3_EXPORT_TRANSFORMERS, _EAGLE3_EXPORT_EXTRA)
    except RuntimeError as exc:  # pip failure: an acquisition error, not an incompatibility
        raise PreparationError(str(exc)) from exc


# ----------------------------------------------------------------------------
# Eagle3 compatibility
# ----------------------------------------------------------------------------
def eagle3_ir_interface(xml_path: Path) -> tuple:
    """``({input name: rank}, {output names})`` of an IR, read from its XML."""
    inputs, outputs = {}, set()
    with open(xml_path, "rb") as handle:
        for _, elem in ET.iterparse(handle, events=("end",)):
            if elem.tag != "layer":
                continue
            if elem.get("type") == "Parameter":
                port = elem.find("output/port")
                inputs[elem.get("name")] = len(port.findall("dim")) if port is not None else 0
            elif elem.get("type") == "Result":
                outputs.update(n for n in (elem.get("output_names") or "").split(",") if n)
            elem.clear()
    return inputs, outputs


def validate_eagle3_pair(target_dir: Path, draft_dir: Path) -> None:
    """Check an exported Eagle3 drafter against the target it will run with."""
    draft_cfg = _read_json(draft_dir / "config.json")
    if not any("eagle3" in str(a).lower() for a in draft_cfg.get("architectures") or []):
        raise CompatibilityError("drafter config.json does not declare an Eagle3 architecture")
    target_type = _read_json(target_dir / "config.json").get("model_type")
    if draft_cfg.get("target_model_type") != target_type:
        raise CompatibilityError(
            f"drafter was trained for {draft_cfg.get('target_model_type')!r}, the target is "
            f"{target_type!r}; the drafter belongs to a different model"
        )
    target_cfg = _model_config(target_dir)
    for key in ("hidden_size", "vocab_size"):
        drafted, target = draft_cfg.get(key), target_cfg.get(key)
        if isinstance(drafted, int) and isinstance(target, int) and drafted != target:
            raise CompatibilityError(
                f"drafter {key} ({drafted}) does not match the target ({target})")
    xml_path = draft_dir / "openvino_model.xml"
    inputs, outputs = eagle3_ir_interface(xml_path)
    missing = [n for n in _EAGLE3_INPUTS if n not in inputs]
    missing += [n for n in _EAGLE3_OUTPUTS if n not in outputs]
    if missing:
        raise CompatibilityError(
            f"drafter IR is not a VLM Eagle3 export (missing {', '.join(missing)}); "
            "re-export it with the qualified recipe")
    if inputs["position_ids"] != 3:
        raise CompatibilityError(
            f"drafter position_ids has rank {inputs['position_ids']}, not the 3D M-RoPE ids "
            "a Qwen-VL target passes")
    if not has_model_rt_info(xml_path, EAGLE3_DRAFT_RT_INFO_KEY):
        raise CompatibilityError(f"drafter IR lacks the {EAGLE3_DRAFT_RT_INFO_KEY} marker")


# ----------------------------------------------------------------------------
# DFlash compatibility and the derived target
# ----------------------------------------------------------------------------
def _model_config(model_dir: Path) -> dict:
    cfg = _read_json(model_dir / "config.json")
    text = cfg.get("text_config")
    return text if isinstance(text, dict) and text else cfg


def dflash_settings(draft_dir: Path) -> dict:
    section = _read_json(draft_dir / "config.json").get("dflash_config")
    return section if isinstance(section, dict) else {}


def validate_dflash_pair(target_dir: Path, draft_dir: Path, k: int) -> list:
    """Check drafter metadata against the target; returns the target layer ids."""
    section = dflash_settings(draft_dir)
    layer_ids = section.get("target_layer_ids")
    if (not isinstance(layer_ids, list) or not layer_ids
            or any(not isinstance(i, int) or isinstance(i, bool) or i < 0 for i in layer_ids)
            or len(set(layer_ids)) != len(layer_ids)):
        raise CompatibilityError(
            "drafter config.json must declare dflash_config.target_layer_ids as unique "
            "non-negative integers"
        )
    target_cfg = _model_config(target_dir)
    depth = target_cfg.get("num_hidden_layers")
    if isinstance(depth, int) and max(layer_ids) >= depth:
        raise CompatibilityError(
            f"drafter reads target layer {max(layer_ids)} but the target has {depth} layers; "
            "the drafter belongs to a different model"
        )
    draft_vocab = _model_config(draft_dir).get("vocab_size")
    target_vocab = target_cfg.get("vocab_size")
    if isinstance(draft_vocab, int) and isinstance(target_vocab, int) and draft_vocab != target_vocab:
        raise CompatibilityError(
            f"drafter vocabulary ({draft_vocab}) does not match the target ({target_vocab})"
        )
    block_size = section.get("block_size")
    if isinstance(block_size, int) and not isinstance(block_size, bool) and block_size > 1:
        if k > block_size - 1:
            raise CompatibilityError(
                f"num_assistant_tokens={k} exceeds the drafter's block_size {block_size} "
                f"minus the seed token (max {block_size - 1})"
            )
    return layer_ids


def target_language_xml(target_dir: Path) -> Path:
    for name in _LANGUAGE_MODEL_XML:
        if (target_dir / name).is_file():
            return target_dir / name
    raise CompatibilityError(f"no language-model IR in {_rel(target_dir)}")


def has_model_rt_info(xml_path: Path, key: str) -> bool:
    """Whether the IR's model-level ``<rt_info>`` (a direct child of ``<net>``) has ``key``."""
    depth = 0
    in_model_rt_info = False
    # An explicit handle: returning from inside iterparse must not leave the file open.
    with open(xml_path, "rb") as handle:
        for event, elem in ET.iterparse(handle, events=("start", "end")):
            if event == "start":
                depth += 1
                if depth == 2 and elem.tag == "rt_info":
                    in_model_rt_info = True
                elif in_model_rt_info and depth == 3 and elem.tag == key:
                    return True
            else:
                if depth == 2 and elem.tag == "rt_info":
                    in_model_rt_info = False
                depth -= 1
                if depth >= 2:
                    elem.clear()
    return False


def dflash_layer_annotation(root: ET.Element, layer_ids: list) -> str:
    """GenAI's hidden-state locator: each target layer's final residual Add."""
    layers_node = root.find("layers")
    if layers_node is None:
        raise CompatibilityError("target IR has no <layers> section")
    located = {}
    for layer_id in layer_ids:
        suffix = f"layers.{layer_id}/aten::add/Add_1"
        matches = [l for l in layers_node.findall("layer")
                   if (l.get("name") or "").endswith(suffix)]
        if len(matches) != 1:
            raise CompatibilityError(
                f"target layer {layer_id}: expected one residual node ending in {suffix!r}, "
                f"found {len(matches)}"
            )
        port = matches[0].find("output/port")
        rank = len(port.findall("dim")) if port is not None else 0
        if rank != 3:
            raise CompatibilityError(f"target layer {layer_id} residual has rank {rank}, not 3")
        located[str(layer_id)] = {"producer": matches[0].get("name"), "output_index": 0}
    return json.dumps({"layers": located}, separators=(",", ":"))


def ensure_dflash_target(target_dir: Path, draft_dir: Path) -> Path:
    """Return a target directory whose IR carries the DFlash hidden-state metadata.

    A current export already has it and is used as-is. An older one gets a
    derived sibling ``<weight>-dflash``: weights hard-linked (copied if the
    filesystem cannot link), language-model XML annotated through ElementTree.
    The ordinary target stays byte-identical for plain generation.
    """
    xml_path = target_language_xml(target_dir)
    if has_model_rt_info(xml_path, DFLASH_TARGET_RT_INFO_KEY):
        return target_dir

    derived = target_dir.with_name(target_dir.name + "-dflash")
    source_hash = sha256_file(xml_path)
    layer_ids = validate_dflash_pair(target_dir, draft_dir, 1)

    def current() -> bool:
        # The annotation names the drafter's layers: another drafter needs a new one.
        stamp = _read_json(derived / ".derived.json")
        return (stamp.get("source_xml_sha256") == source_hash
                and stamp.get("target_layer_ids") == layer_ids
                and (derived / xml_path.name).is_file())

    if current():
        return derived
    with ArtifactLock(derived):
        if current():  # another start built it while this one waited
            return derived
        if derived.exists():
            _set_aside(derived, "stale")
        staging = derived.with_name(derived.name + ".partial")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        files = [p for p in target_dir.rglob("*") if p.is_file() and p != xml_path
                 and not p.name.startswith(".")]
        _check_disk(staging, 0 if _can_hardlink(target_dir, staging)
                    else sum(p.stat().st_size for p in files))
        for src in files:
            dst = staging / src.relative_to(target_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        tree = ET.parse(str(xml_path))
        root = tree.getroot()
        annotation = dflash_layer_annotation(root, layer_ids)
        rt_info = root.find("rt_info")
        if rt_info is None:
            rt_info = ET.SubElement(root, "rt_info")
        ET.SubElement(rt_info, DFLASH_TARGET_RT_INFO_KEY, {"value": annotation})
        out_xml = staging / xml_path.name
        tree.write(str(out_xml), encoding="utf-8", xml_declaration=True)
        _write_json(staging / ".derived.json", {
            "source": _rel(target_dir),
            "source_xml_sha256": source_hash,
            "derived_xml_sha256": sha256_file(out_xml),
            "rt_info_key": DFLASH_TARGET_RT_INFO_KEY,
            "target_layer_ids": layer_ids,
            "created_at": _now(),
        })
        staging.replace(derived)
    logger.info("Derived DFlash target %s from %s", _rel(derived), _rel(target_dir))
    return derived


# ----------------------------------------------------------------------------
# DFlash drafter with its own lm_head
# ----------------------------------------------------------------------------
_HEAD_GROUP = 128
_HEAD_CHUNK_ROWS = 16384  # rows requantized at once; an f32 copy of the whole head is 2 GB


def ensure_draft_head(target_dir: Path, draft_dir: Path,
                      target_weight: Optional[str] = None) -> Path:
    """Return a drafter that carries its own int4 lm_head, built from the target's.

    An exported DFlash drafter ends at ``last_hidden_state``, so the runtime
    grafts the target's int8 lm_head onto it (509 MB for Qwen3.6) and reads it
    on every draft pass. A drafter that outputs ``logits`` keeps its own head:
    a derived sibling ``<draft>-lmhead`` gets the same head requantized to int4
    symmetric, half the bytes. In context_bench this made verification passes
    2-7% cheaper with acceptance unchanged. Rebuilt when the target or drafter
    IR changes; if it cannot be built, the stock drafter is used.

    The head belongs to the target: a target of another precision than the
    drafter export (``target_weight``) gets its own ``<draft>-lmhead-<weight>``,
    so switching precisions neither rebuilds nor sets aside the other one.
    """
    suffix = f"-{target_weight}" if target_weight and target_weight != draft_dir.name else ""
    derived = draft_dir.with_name(f"{draft_dir.name}-lmhead{suffix}")
    stamp = {
        "target_xml_sha256": sha256_file(target_language_xml(target_dir)),
        "draft_xml_sha256": sha256_file(draft_dir / "openvino_model.xml"),
    }

    def current() -> bool:
        recorded = _read_json(derived / ".derived.json")
        return (all(recorded.get(k) == v for k, v in stamp.items())
                and all((derived / f).is_file() for f in _DRAFT_IR_FILES))

    if current():
        return derived
    try:
        with ArtifactLock(derived):
            if current():  # another start built it while this one waited
                return derived
            staging = derived.with_name(derived.name + ".partial")
            if staging.exists():
                shutil.rmtree(staging)
            if not _build_draft_head(target_dir, draft_dir, staging):
                return draft_dir  # the drafter already has its own head
            _write_json(staging / ".derived.json", {
                **stamp, "source": _rel(draft_dir), "head": f"int4 sym g{_HEAD_GROUP}",
                "created_at": _now()})
            if derived.exists():
                _set_aside(derived, "stale")
            staging.replace(derived)
    except Exception as exc:  # noqa: BLE001 - an optimization; the stock drafter still runs
        logger.warning("Using the stock DFlash drafter; its own lm_head could not be built: %s",
                       exc)
        return draft_dir
    logger.info("Derived DFlash drafter %s with its own int4 lm_head", _rel(derived))
    return derived


def _build_draft_head(target_dir: Path, draft_dir: Path, out_dir: Path) -> bool:
    """Write ``draft_dir`` plus the target's head to ``out_dir``; False if it has one."""
    import numpy as np
    import openvino as ov
    import openvino.opset13 as ops

    model = ov.Core().read_model(str(draft_dir / "openvino_model.xml"))
    if len(model.outputs) != 1:
        raise CompatibilityError("drafter IR must have exactly one output")
    if "logits" in model.outputs[0].get_names():
        return False
    weight, zero_point, scale = _target_lm_head(target_language_xml(target_dir))
    vocab, hidden_size = weight.shape
    result = model.get_results()[0]
    hidden = result.input_value(0)
    if hidden.get_partial_shape()[-1].get_length() != hidden_size or hidden_size % _HEAD_GROUP:
        raise CompatibilityError("drafter hidden size does not match the target lm_head")
    if hidden.get_element_type() != ov.Type.f32:
        hidden = ops.convert(hidden, ov.Type.f32)

    packed, scales = _requantize_int4(weight, zero_point, scale)
    codes = ov.Tensor(ov.Type.i4, ov.Shape([vocab, hidden_size // _HEAD_GROUP, _HEAD_GROUP]))
    np.copyto(codes.data.view(np.uint8).reshape(-1), packed.reshape(-1))
    head = ops.reshape(
        ops.multiply(ops.convert(ops.constant(codes), ov.Type.f16), ops.constant(scales)),
        ops.constant(np.array([vocab, hidden_size], np.int64)), special_zero=False)
    logits = ops.result(ops.matmul(hidden, ops.convert(head, ov.Type.f32), False, True))
    logits.output(0).get_tensor().set_names({"logits"})
    model.add_results([logits])
    model.remove_result(result)
    model.validate_nodes_and_infer_types()

    out_dir.mkdir(parents=True)
    ov.save_model(model, str(out_dir / "openvino_model.xml"), compress_to_fp16=False)
    for src in draft_dir.iterdir():
        if src.is_file() and not src.name.startswith(("openvino_model.", ".")):
            shutil.copy2(src, out_dir / src.name)
    return True


def _target_lm_head(xml_path: Path) -> tuple:
    """``(weight, zero_point or None, scale)`` of the target's per-row int8 lm_head.

    Memory-mapped from the ``.bin``, so the target is never loaded. Accepts the
    chains optimum-intel writes into ``MatMul(transpose_b)`` named ``lm_head``:
    ``Convert(Multiply(Convert(i8), scale))`` or, for an asymmetric head,
    ``Convert(Multiply(Subtract(Convert(u8), Convert(u8 zero point)), scale))``.
    """
    import numpy as np

    root = ET.parse(str(xml_path)).getroot()
    layers = {layer.get("id"): layer for layer in root.iter("layer")}
    producers: dict = {}
    for edge in root.iter("edge"):
        producers.setdefault(edge.get("to-layer"), []).append(layers[edge.get("from-layer")])

    def inputs(layer, kind):
        return [p for p in producers.get(layer.get("id"), []) if p.get("type") == kind]

    def one(layer, kind):
        found = inputs(layer, kind)
        if len(found) != 1:
            raise CompatibilityError(f"unsupported lm_head layout at {layer.get('name')}")
        return found[0]

    heads = [layer for layer in layers.values() if layer.get("type") == "MatMul"
             and "lm_head" in (layer.get("name") or "")]
    if len(heads) != 1 or heads[0].find("data").get("transpose_b") != "true":
        raise CompatibilityError("target has no single transposed lm_head MatMul")
    multiply = one(one(heads[0], "Convert"), "Multiply")
    scale = one(multiply, "Const")
    subtract = inputs(multiply, "Subtract")
    if subtract:
        zero_point, weight = sorted((one(c, "Const") for c in inputs(subtract[0], "Convert")),
                                    key=lambda const: _shape(const)[-1])
    else:
        zero_point, weight = None, one(one(multiply, "Convert"), "Const")

    bin_path = xml_path.with_suffix(".bin")
    dtypes = {"i8": np.int8, "u8": np.uint8, "f16": np.float16}

    def array(const):
        data = const.find("data")
        if data.get("element_type") not in dtypes:
            raise CompatibilityError(
                f"unsupported lm_head element type {data.get('element_type')}")
        return np.memmap(bin_path, dtypes[data.get("element_type")], "r",
                         offset=int(data.get("offset")), shape=_shape(const))

    vocab = _shape(weight)[0]
    per_row = [c for c in (scale, zero_point) if c is not None]
    if len(_shape(weight)) != 2 or any(_shape(c) != (vocab, 1) for c in per_row):
        raise CompatibilityError("target lm_head is not int8 with per-row scales")
    return array(weight), (array(zero_point) if zero_point is not None else None), array(scale)


def _shape(const) -> tuple:
    return tuple(int(x) for x in const.find("data").get("shape").split(","))


def _requantize_int4(weight, zero_point, scale) -> tuple:
    """Int4 symmetric codes (two per byte, low nibble first) and f16 group scales."""
    import numpy as np

    vocab, hidden_size = weight.shape
    groups = hidden_size // _HEAD_GROUP
    packed = np.empty((vocab, groups, _HEAD_GROUP // 2), np.uint8)
    scales = np.empty((vocab, groups, 1), np.float16)
    for start in range(0, vocab, _HEAD_CHUNK_ROWS):
        stop = min(vocab, start + _HEAD_CHUNK_ROWS)
        rows = weight[start:stop].astype(np.float32)
        if zero_point is not None:
            rows -= zero_point[start:stop].astype(np.float32)
        rows = (rows * scale[start:stop].astype(np.float32)).reshape(stop - start, groups, -1)
        step = np.abs(rows).max(axis=2, keepdims=True) / 7.0
        step[step == 0] = 1.0
        step = step.astype(np.float16)  # codes are chosen against the scale actually stored
        nibbles = (np.clip(np.rint(rows / step.astype(np.float32)), -8, 7).astype(np.int8)
                   & 0x0F).astype(np.uint8)
        packed[start:stop] = nibbles[..., 0::2] | (nibbles[..., 1::2] << 4)
        scales[start:stop] = step
    return packed, scales


def _can_hardlink(src_dir: Path, dst_dir: Path) -> bool:
    try:
        return os.stat(src_dir).st_dev == os.stat(dst_dir).st_dev
    except OSError:
        return False


# ----------------------------------------------------------------------------
# Hub access, locking, files
# ----------------------------------------------------------------------------
def _snapshot(repo: str, local_dir: Path, revision: Optional[str], allow_patterns=None):
    from huggingface_hub import snapshot_download

    return snapshot_download(repo_id=repo, revision=revision, local_dir=str(local_dir),
                             allow_patterns=allow_patterns)


def _resolve_revision(repo: str) -> str:
    from huggingface_hub import HfApi

    sha = HfApi().model_info(repo).sha
    if not sha or not _COMMIT_RE.match(sha):
        raise PreparationError(f"could not resolve an immutable revision for {repo}")
    logger.warning("No pinned revision for %s in the catalog; using current commit %s "
                   "and recording it in the manifest", repo, sha[:12])
    return sha


def _repo_size(repo: str, revision: str) -> int:
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    return sum(int(getattr(s, "size", 0) or 0) for s in (info.siblings or []))


def _with_retries(fn, what: str, attempts: int = DOWNLOAD_ATTEMPTS):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (PreparationError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - network/hub errors are retried
            last = exc
            logger.warning("Fetching %s failed (attempt %d/%d): %s", what, attempt,
                           attempts, exc)
            if attempt < attempts:
                time.sleep(min(30, 5 * attempt))
    raise PreparationError(f"could not fetch {what}: {last}") from last


def _check_disk(path: Path, needed_bytes: float) -> None:
    if not needed_bytes:
        return
    free = shutil.disk_usage(str(path)).free
    if free < needed_bytes:
        raise PreparationError(
            f"insufficient disk space: {needed_bytes / 2**30:.1f} GiB needed, "
            f"{free / 2**30:.1f} GiB free"
        )


class ArtifactLock:
    """Exclusive per-artifact lock file so concurrent starts do not interleave."""

    def __init__(self, artifact: Path, timeout_s: float = LOCK_TIMEOUT_S) -> None:
        self.path = artifact.with_name(artifact.name + ".lock")
        self.timeout_s = timeout_s
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, f"{os.getpid()} {_own_start()}".strip().encode("ascii"))
                return self
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise PreparationError(f"timed out waiting for {self.path.name}")
                time.sleep(2.0)

    def _stale(self) -> bool:
        """Whether the owner is gone: no such process, or a reused process id."""
        try:
            owner = self.path.read_text(encoding="ascii").split()
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if not owner:
            # The owner died between creating the file and writing its id.
            return age > _EMPTY_LOCK_GRACE_S
        try:
            pid = int(owner[0])
        except ValueError:
            return True
        if pid == os.getpid():
            return False
        try:
            started = _process_started(pid)
        except ImportError:  # psutil unavailable: never steal a lock
            return False
        # Windows reuses process ids: the start time tells a new process apart
        # ("" when it cannot be read, which never counts as different).
        return started is None or bool(started and len(owner) > 1 and owner[1] != started)

    def __exit__(self, *exc):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


_EMPTY_LOCK_GRACE_S = 60.0


def _own_start() -> str:
    try:
        return _process_started(os.getpid()) or ""
    except ImportError:
        return ""


def _process_started(pid: int) -> Optional[str]:
    """Start time of process ``pid`` (whole seconds); "" if unreadable, None if gone."""
    import psutil

    try:
        return f"{psutil.Process(pid).create_time():.0f}"
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None if not psutil.pid_exists(pid) else ""


def _set_aside(path: Path, why: str) -> None:
    """Move an artifact out of the active path instead of deleting it (rollback)."""
    target = path.with_name(f"{path.name}.{why}-{datetime.now():%Y%m%d%H%M%S}")
    path.replace(target)
    logger.warning("Moved %s aside to %s", _rel(path), _rel(target))


def _require_files(directory: Path, names, what: str) -> None:
    missing = [n for n in names if not (directory / n).is_file()]
    if missing:
        raise PreparationError(f"{what} is missing {', '.join(missing)}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_sizes(directory: Path) -> dict:
    return {str(p.relative_to(directory)).replace("\\", "/"): p.stat().st_size
            for p in sorted(directory.rglob("*")) if p.is_file() and not p.name.startswith(".")}


def _read_json(path: Path, *, lenient: bool = False) -> dict:
    """The JSON object in ``path``; ``{}`` if the file is missing or not a JSON object.

    A file that exists but cannot be read raises :class:`PreparationError`: read
    as ``{}`` it would pass for absent metadata, as when a drafter exported from
    an elevated session was reported as declaring no DFlash target layers.
    ``lenient`` reads optional bookkeeping (the manifest) as if it were missing.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return {}
    except OSError as exc:
        if lenient:
            return {}
        reason = ("not readable by this user" if isinstance(exc, PermissionError)
                  else f"unreadable: {exc.strerror or exc}")
        raise PreparationError(f"{_rel(path)} is {reason}") from exc
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _rel(path: Path) -> str:
    """Root-relative path for logs and manifests; never a user's absolute path."""
    try:
        return str(Path(path).resolve().relative_to(SC_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return Path(path).name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------------
def manifest_path(plan: TextGenPlan) -> Path:
    return MANIFEST_DIR / f"{plan.target_dir.parent.name}-{plan.weight_format}.json"


def _recorded_draft_revision(plan: TextGenPlan, repo: Optional[str]) -> Optional[str]:
    spec = _read_json(manifest_path(plan), lenient=True).get("speculative") or {}
    if repo and str(spec.get("draft_model", "")).lower() == repo.lower():
        revision = spec.get("draft_revision")
        return revision if isinstance(revision, str) and _COMMIT_RE.match(revision) else None
    return None


def _write_manifest(result: PreparedArtifacts) -> Optional[Path]:
    plan = result.plan
    path = manifest_path(plan)
    previous = _read_json(path, lenient=True)
    data = {
        "schema_version": 1,
        "updated_at": _now(),
        "target": {
            "model": plan.model_name,
            "weight_format": plan.weight_format,
            "dir": _rel(result.target_dir),
            "repo": preconverted_repo(plan.model_name, plan.weight_format),
            # The existing download path does not record provenance; a legacy
            # cache is reused without claiming a revision nobody verified.
            "revision": None,
        },
        "speculative": {
            "mode": plan.mode,
            "requested_method": plan.requested_method,
            "status": "ready" if result.speculative else (
                "unavailable" if plan.mode != "off" else "off"),
            "reason": result.speculative_error,
        },
        "timings": result.timings,
    }
    spec = result.speculative
    if spec is not None:
        data["speculative"].update({
            "method": spec.settings.method,
            "draft_source": spec.settings.source,
            "draft_model": (spec.settings.draft_model
                            if spec.settings.source == "huggingface" else spec.settings.describe()),
            "draft_revision": spec.draft_revision,
            "num_assistant_tokens": spec.settings.num_assistant_tokens,
            "draft_dir": _rel(spec.draft_dir),
            "target_dir": _rel(spec.target_dir),
        })
    elif isinstance(previous.get("speculative"), dict) and previous["speculative"].get("draft_revision"):
        # Keep the recorded pin so a later accelerated start reuses it.
        for key in ("method", "draft_source", "draft_model", "draft_revision"):
            if key in previous["speculative"]:
                data["speculative"].setdefault(key, previous["speculative"][key])
    try:
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        _write_json(path, data)
        return path
    except OSError as exc:
        logger.warning("Could not write text_gen manifest: %s", exc)
        return None

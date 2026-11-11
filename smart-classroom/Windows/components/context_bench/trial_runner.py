# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Runs one benchmark case in an isolated, spawned child process.

The case is an immutable, serializable mapping prepared by `benchmark.py`. The child loads
the local tokenizer and pipeline once, builds and validates the prompt(s), then runs either
``warmup + iterations`` generations over one reused prompt (throughput) or one generation
per accuracy probe. Prompt construction, the idle gap and every acknowledgement wait sit
outside the timed `generate` call.

Protocol: events go to the parent on `result_queue`, each carrying the case ID. Before every
generate the child posts `iteration_start` and waits for the parent's ``ready``
acknowledgement (the parent has opened the iteration's memory window); after posting the
`iteration`/`probe` record it waits for ``recorded`` (the parent has closed the window and
checkpointed). Acknowledgements arrive on a separate `control_queue` and are bounded by the
case's progress timeout; a missing or broken channel ends the case as an orchestration
failure. The terminal `done` message is posted through `_post_and_exit`, which flushes the
queue and leaves via ``os._exit`` without running OpenVINO's pipeline destructors.

Completed milestones are ``start -> loaded -> prompt_built -> prefilled -> decoded``. They
are reset per iteration -- to ``loaded`` before building each probe and to ``prompt_built``
before every generate, warm-up included -- so a later prefill failure never inherits the
``decoded`` of an earlier call.

Timing comes from OpenVINO GenAI's own ``perf_metrics`` where valid, falling back per field
to wall-clock timing around `generate` and the first streamer callback; the two clocks are
never mixed (see `metrics.select_timing`).

Artifacts are only read: the tokenizer loads with ``local_files_only=True`` and
``trust_remote_code=False``, and a DFlash target lacking hidden-state metadata is rejected
rather than repaired (the serving preparation derives a `<weight_format>-dflash` target).

OpenVINO / transformers are imported lazily inside the child functions so this module can
be imported, and tested, without the OpenVINO stack.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path
from queue import Empty

from components.context_bench import metrics
from components.context_bench.context_builder import (
    PromptSizeError,
    build_benchmark_prompt,
    build_probe_prompt,
)
from components.context_bench.tasks import word_list_ranking_holds
from components.vlm.text_gen_profiles import DFLASH_TARGET_RT_INFO_KEY

# How far an accuracy probe's prompt may land from the configured context, per tokenizer
# count. Throughput stays exact; a retrieval verdict does not depend on one token, and the
# count actually used is recorded.
PROBE_TOKEN_TOLERANCE = 16

_OOM_MARKERS = (
    "out of gpu resources",
    "out of memory",
    "allocation failed",
    "bad_alloc",
    "cannot allocate",
    "can't allocate",
    "insufficient memory",
    "memoryerror",
    "larger than available memory",  # scheduler refusing an oversized cache_size
    "did not fit in the available cache budget",  # scheduler cache-admission failure
    "cl_mem_object_allocation_failure",  # -4
    "cl_out_of_host_memory",  # -6
    "cl_invalid_buffer_size",  # -61
)

# A GPU command that was accepted and then died on the device. A driver reset produces the
# same code as exhaustion, so this is reported as an abort, not as a confirmed OOM.
_GPU_ABORT_MARKERS = (
    "cl_exec_status_error_for_events_in_wait_list",  # -14
    "cl_out_of_resources",  # -5
    "cl_invalid_command_queue",  # -36
    "clwaitforevents",
    "clfinish",
)

_UNSUPPORTED_MARKERS = (
    "unsupported property",
    "unsupported configuration",
    "is not supported",
    "not supported by",
    "unknown property",
    "unsupported schedulerconfig property",
)

STAGE_START = "start"
STAGE_LOADED = "loaded"
STAGE_PROMPT_BUILT = "prompt_built"
STAGE_PREFILLED = "prefilled"
STAGE_DECODED = "decoded"

# The stage a failure belongs to, given the last completed milestone: an exception is thrown
# by the step *after* it.
_FAILING_STAGE = {
    STAGE_START: "load",
    STAGE_LOADED: "prompt",
    STAGE_PROMPT_BUILT: "prefill",
    STAGE_PREFILLED: "decode",
    STAGE_DECODED: "decode",
}

ACK_READY = "ready"
ACK_RECORDED = "recorded"
EVENTS = ("device", "loaded", "prompt", "iteration_start", "prefilled", "iteration", "probe",
          "done")

LANGUAGE_MODEL_IRS = ("openvino_language_model.xml", "openvino_model.xml")

PASS_PROFILE_FIELDS = ("first_decode_ms", "steady_pass_ms", "steady_tpot")

# Properties openvino_genai consumes itself rather than forwarding to the plugin; the plugin
# does not advertise them, so they are not reported as unadvertised.
_GENAI_LEVEL_PROPERTIES = frozenset({"ATTENTION_BACKEND", "scheduler_config", "draft_model"})

_WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"<>|,;)]*")
_POSIX_PATH = re.compile(r"(?<![^\s'\"(=])/(?:[^\s'\"/,;)]+/)+[^\s'\",;)]*")


class OrchestrationError(RuntimeError):
    """An acknowledgement or reporting failure between child and parent."""


def failing_stage(stage_reached: str) -> str:
    """Name the stage that was running when a case at `stage_reached` failed."""
    return _FAILING_STAGE.get(stage_reached, "generate")


def sanitize_message(text, limit: int = 400, keep_newlines: bool = False) -> str:
    """A diagnostic without absolute paths (which carry user names) and bounded in length."""
    # The home directory first: the path patterns stop at a space in a user name.
    text, home = str(text or ""), os.path.expanduser("~")
    if home != "~":
        text = text.replace(home, "<home>")
    text = _WINDOWS_PATH.sub("<path>", text)
    text = _POSIX_PATH.sub("<path>", text)
    if keep_newlines:
        text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    else:
        text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Artifact checks (pure filesystem/XML reads)
# ---------------------------------------------------------------------------
def dflash_block_size(draft_model_dir) -> int | None:
    """The draft's trained block size (seed token included), or None if not declared."""
    from components.vlm.text_gen_prepare import dflash_settings

    block_size = dflash_settings(Path(draft_model_dir)).get("block_size")
    if isinstance(block_size, int) and not isinstance(block_size, bool) and block_size > 1:
        return block_size
    return None


def language_model_ir(model_dir: str) -> str | None:
    """The IR carrying a directory's language model: VLM layout first, then plain LLM."""
    for name in LANGUAGE_MODEL_IRS:
        path = os.path.join(model_dir, name)
        if os.path.isfile(path):
            return path
    return None


def has_model_rt_info(xml_path: str, key: str) -> bool | None:
    """Whether the IR's model-level ``<rt_info>`` (a direct child of ``<net>``) has `key`.

    The serving preparation's own check; None when the XML cannot be read.
    """
    from components.vlm.text_gen_prepare import has_model_rt_info as serving_has_model_rt_info

    try:
        return serving_has_model_rt_info(xml_path, key)
    except (ET.ParseError, OSError):
        return None


DFLASH_METADATA_HINT = (
    f"the target IR lacks model rt_info {DFLASH_TARGET_RT_INFO_KEY!r}. The benchmark never "
    "repairs artifacts; point model.model_dirs at the derived target the serving "
    "preparation creates when DFlash is enabled (<weight_format>-dflash, e.g. "
    "models/openvino/Qwen3.6-35B-A3B/int4-dflash)"
)


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, OrchestrationError):
        return "orchestration"
    text = str(exc).lower()
    if text.startswith("no_output"):
        return "no_output"
    if any(marker in text for marker in _UNSUPPORTED_MARKERS):
        return "unsupported"
    if any(marker in text for marker in _OOM_MARKERS):
        return "oom"
    if any(marker in text for marker in _GPU_ABORT_MARKERS):
        return "gpu_abort"
    if isinstance(exc, PromptSizeError):
        return "prompt"
    return "error"


# ---------------------------------------------------------------------------
# Runtime (lazy imports)
# ---------------------------------------------------------------------------
def _device_diagnostics(device: str, ov_config: dict) -> dict:
    """What the plugin says about `device` and the properties it is handed.

    Read in the child: initializing a GPU context in the parent would pollute its memory
    baseline. Driver-reported total memory is descriptive only. Best-effort: a failure yields
    unknown values, never a failed case.
    """
    info = {"gpu_budget_gb": None, "unsupported_properties": [], "device_name": None}
    try:
        import openvino as ov

        core = ov.Core()
        if ov_config:
            supported = set(core.get_property(device, "SUPPORTED_PROPERTIES"))
            info["unsupported_properties"] = [
                k for k in ov_config
                if k not in supported and k not in _GENAI_LEVEL_PROPERTIES
            ]
        try:
            info["device_name"] = str(core.get_property(device, "FULL_DEVICE_NAME"))
        except Exception:  # noqa: BLE001
            pass
        if str(device).upper().startswith("GPU"):
            total_bytes = core.get_property(device, "GPU_DEVICE_TOTAL_MEM_SIZE")
            info["gpu_budget_gb"] = float(total_bytes) / (1024 ** 3)
    except Exception:  # noqa: BLE001 - diagnostics must never fail a case
        pass
    return info


def _load_tokenizer(model_dir: str):
    """Load the local HF tokenizer used to size prompts, without network or remote code.

    Tries progressively more permissive loaders for two known `optimum-cli` export quirks
    (`extra_special_tokens` written as a list, an unresolvable `tokenizer_class`). Every
    attempt is local-only and never trusts repository code.
    """
    from transformers import AutoTokenizer, PreTrainedTokenizerFast
    from transformers.utils import logging as hf_logging

    hf_logging.set_verbosity_error()

    attempts = [
        (AutoTokenizer, {}),
        (AutoTokenizer, {"extra_special_tokens": {}}),
        (PreTrainedTokenizerFast, {}),
        (PreTrainedTokenizerFast, {"extra_special_tokens": {}}),
    ]
    last_exc = None
    for tokenizer_cls, extra_kwargs in attempts:
        try:
            return tokenizer_cls.from_pretrained(
                model_dir, local_files_only=True, trust_remote_code=False, **extra_kwargs
            )
        except (ValueError, AttributeError, OSError) as exc:
            last_exc = exc
    raise last_exc


def _load_pipeline(model_dir: str, device: str, ov_config: dict,
                   scheduler_config: dict | None, mtp: dict | None = None):
    """Construct LLMPipeline or VLMPipeline from the IR layout on disk.

    Returns ``(pipeline, accepts_tokenized_input)``. An empty `scheduler_config` selects
    the stateful pipeline and no SchedulerConfig is instantiated; any key selects paged
    attention. A plain profile passes no draft; DFlash and Eagle3 pass their external draft
    directory to ``ov_genai.draft_model``.
    """
    import openvino_genai as ov_genai

    pipeline_args = dict(ov_config)
    if scheduler_config:
        scheduler = ov_genai.SchedulerConfig()
        for key, value in scheduler_config.items():
            if not hasattr(scheduler, key):
                raise ValueError(f"Unsupported SchedulerConfig property: {key}")
            try:
                setattr(scheduler, key, value)
            except TypeError as exc:
                raise ValueError(
                    f"SchedulerConfig.{key} rejected {value!r} ({type(value).__name__}): {exc}"
                ) from exc
        pipeline_args["scheduler_config"] = scheduler

    if (mtp or {}).get("enabled"):
        pipeline_args["draft_model"] = ov_genai.draft_model(
            mtp["model"], device
        )

    if os.path.exists(os.path.join(model_dir, "openvino_language_model.xml")):
        return ov_genai.VLMPipeline(model_dir, device=device, **pipeline_args), False
    if os.path.exists(os.path.join(model_dir, "openvino_model.xml")):
        return ov_genai.LLMPipeline(model_dir, device=device, **pipeline_args), True
    raise RuntimeError(
        "Unrecognized OpenVINO IR layout: expected openvino_model.xml (plain LLM) or "
        "openvino_language_model.xml (multimodal/VLM export)"
    )


def prepare_pipeline_input(pipe, prompt: str, accepts_tokenized_input: bool):
    """Tokenize once with the pipeline tokenizer and return the exact inference input.

    LLMPipeline receives the counted TokenizedInputs; VLMPipeline only accepts text, so the
    validated string is returned and the pipeline tokenizes it (with its own chat template
    disabled through the generation config).
    """
    tokenized = pipe.get_tokenizer().encode(prompt)
    prompt_tokens = int(tokenized.input_ids.shape[-1])
    return (tokenized if accepts_tokenized_input else prompt), prompt_tokens


def generation_config(output_tokens: int, mtp: dict | None = None):
    """A fresh greedy request for one generate call (plain or speculative).

    Fresh per call: the model's generation_config.json may enable sampling, and reusing a
    pipeline-owned config risks retaining fields from an earlier request. The pipeline's own
    chat-template application is disabled because the prompt is already rendered. EOS is
    respected.
    """
    import openvino_genai as ov_genai

    config = ov_genai.GenerationConfig()
    config.max_new_tokens = output_tokens
    config.do_sample = False
    config.ignore_eos = False
    config.num_return_sequences = 1
    config.assistant_confidence_threshold = 0.0
    config.num_assistant_tokens = (
        mtp["num_assistant_tokens"] if (mtp or {}).get("enabled") else 0
    )
    if hasattr(config, "apply_chat_template"):
        config.apply_chat_template = False
    return config


def generated_token_count(result, tokenizer) -> int:
    """Count encoded output tokens, falling back to pipeline-tokenizer text encoding."""
    tokens = getattr(result, "tokens", None)
    if tokens is not None:
        try:
            return len(tokens[0])
        except (IndexError, TypeError):
            pass
    output_text = generated_text(result, tokenizer)
    return int(tokenizer.encode(output_text).input_ids.shape[-1])


def generated_text(result, tokenizer) -> str:
    """Output text; LLMPipeline given TokenizedInputs returns only tokens."""
    texts = getattr(result, "texts", None)
    if texts:
        return texts[0]
    tokens = getattr(result, "tokens", None)
    if tokens:
        return tokenizer.decode(list(tokens[0]), skip_special_tokens=True)
    return str(result)


def _mean_ms(pair) -> float | None:
    """A MeanStdPair's mean if finite and non-negative; None if the runtime left it unset."""
    return metrics.non_negative_number(getattr(pair, "mean", None))


def read_perf_metrics(result) -> dict:
    """OpenVINO GenAI's own measurement of one generate() call, in llm_bench's units.

    Every field is optional and validated; the caller falls back per field. `input_size`
    is the runtime's count of what it prefilled, which on the VLMPipeline path need not
    equal the prompt measured before the call.
    """
    out = {}
    perf = getattr(result, "perf_metrics", None)
    if perf is None:
        return out
    for key, getter in (
        ("first_token_latency", "get_ttft"),
        ("other_tokens_avg_latency", "get_tpot"),
        ("tokenization_time", "get_tokenization_duration"),
        ("detokenization_time", "get_detokenization_duration"),
    ):
        try:
            value = _mean_ms(getattr(perf, getter)())
        except Exception:  # noqa: BLE001 - a missing metric is not a failed case
            value = None
        if value is not None:
            out[key] = value
    try:
        generated = perf.get_num_generated_tokens()
        if isinstance(generated, int) and not isinstance(generated, bool) and generated > 0:
            out["output_size"] = generated
    except Exception:  # noqa: BLE001
        pass
    try:
        consumed = perf.get_num_input_tokens()
        if isinstance(consumed, int) and not isinstance(consumed, bool) and consumed > 0:
            out["input_size"] = consumed
    except Exception:  # noqa: BLE001
        pass
    try:
        duration = metrics.positive_number(_mean_ms(perf.get_generate_duration()))
        if duration is not None:
            out["generation_time"] = duration / 1000.0
    except Exception:  # noqa: BLE001
        pass
    extended = getattr(result, "extended_perf_metrics", None)
    if extended is not None:
        try:
            acceptance = metrics.finite_number(extended.get_draft_acceptance_rate())
            if acceptance is not None and 0.0 <= acceptance <= 1.0:
                out["mtp_acceptance_rate"] = acceptance
        except Exception:  # noqa: BLE001
            pass
        for key, getter in (
            ("mtp_draft_tokens", "get_num_draft_tokens"),
            ("mtp_accepted_tokens", "get_num_accepted_tokens"),
        ):
            try:
                count = metrics.count_value(getattr(extended, getter)())
                if count is not None:
                    out[key] = count
            except Exception:  # noqa: BLE001
                pass
        # The DFlash continuous-batching path leaves get_num_draft_tokens() at 0 but counts
        # every proposed candidate in get_num_draft_processed_tokens().
        if not out.get("mtp_draft_tokens"):
            try:
                processed = metrics.count_value(extended.get_num_draft_processed_tokens())
                if processed:
                    out["mtp_draft_tokens"] = processed
            except Exception:  # noqa: BLE001
                pass
        try:
            rejected = metrics.count_value(extended.get_num_rejected_tokens())
            if rejected is not None:
                out["mtp_rejected_tokens"] = rejected
        except Exception:  # noqa: BLE001
            pass
        drafted, accepted = out.get("mtp_draft_tokens"), out.get("mtp_accepted_tokens")
        if drafted is not None and accepted is not None and accepted <= drafted:
            out["mtp_rejected_tokens"] = drafted - accepted
        try:
            ratio = metrics.non_negative_number(
                extended.get_draft_to_main_inference_duration_ratio()
            )
            if ratio is not None:
                out["mtp_draft_to_main_ratio"] = ratio
        except Exception:  # noqa: BLE001
            pass
    try:
        steps = len(perf.raw_metrics.m_new_token_times)
        if steps > 0:
            out["verification_steps"] = steps
    except Exception:  # noqa: BLE001
        pass
    # Per-pass inference durations (microseconds) and the tokens each pass committed.
    try:
        raw = perf.raw_metrics
        profile = metrics.pass_profile(
            [duration / 1000.0 for duration in raw.token_infer_durations],
            list(raw.m_batch_sizes),
        )
        out.update({k: v for k, v in zip(PASS_PROFILE_FIELDS, profile) if v is not None})
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# Parent channel and progress
# ---------------------------------------------------------------------------
class _Channel:
    """Events to the parent and acknowledgements back, both tagged with the case ID."""

    def __init__(self, case_id: str, result_queue, control_queue, timeout_sec: float,
                 clock=time.monotonic):
        self.case_id = case_id
        self._results = result_queue
        self._control = control_queue
        self._timeout = float(timeout_sec)
        self._clock = clock

    def post(self, event: str, **payload) -> None:
        try:
            self._results.put({**payload, "event": event, "case_id": self.case_id})
        except Exception as exc:  # noqa: BLE001
            raise OrchestrationError(f"result channel failed: {type(exc).__name__}") from exc

    def wait_ack(self, iteration, kind: str) -> None:
        """Block until the parent acknowledges `kind` for `iteration`, within the timeout."""
        if self._control is None:
            return
        deadline = self._clock() + self._timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise OrchestrationError(
                    f"no {kind!r} acknowledgement for iteration {iteration} within "
                    f"{self._timeout:g}s"
                )
            try:
                message = self._control.get(timeout=min(remaining, 1.0))
            except Empty:
                continue
            except (EOFError, OSError, ValueError) as exc:
                raise OrchestrationError(f"control channel closed: {type(exc).__name__}") from exc
            if not isinstance(message, dict) or message.get("case_id") != self.case_id:
                continue
            if message.get("iteration") == iteration and message.get("ack") == kind:
                return


class _Progress:
    """Last completed milestone and the active iteration/phase, separate from counts."""

    def __init__(self):
        self.milestone = STAGE_START
        self.phase = "load"
        self.active_iteration = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _error(exc: BaseException, progress: _Progress, category: str | None = None) -> dict:
    return {
        "category": category or classify_error(exc),
        "stage": failing_stage(progress.milestone),
        "milestone": progress.milestone,
        "phase": progress.phase,
        "iteration": progress.active_iteration,
        "message": sanitize_message(f"{type(exc).__name__}: {exc}"),
    }


def _log_failure(what: str) -> None:
    print(f"[trial_runner] {what} failed:\n"
          + sanitize_message(traceback.format_exc(), limit=4000, keep_newlines=True),
          file=sys.stderr)


def _post_and_exit(result_queue, message: dict) -> None:
    """Hand the result to the parent, then leave *without* OpenVINO's teardown. Never returns.

    Destroying a GPU pipeline that has just prefilled a very long context has been observed
    to throw an ov::Exception out of a destructor (std::terminate, exit code 0xC0000409),
    which no Python handler can contain and which previously destroyed an already completed
    measurement. Posting first and skipping the teardown fixes both; the child exists to run
    one case and process exit reclaims its memory.
    """
    try:
        result_queue.put(message)
        # Queue.put() is asynchronous and os._exit() skips the feeder-thread join: flush.
        result_queue.close()
        result_queue.join_thread()
    except Exception:  # noqa: BLE001 - the parent's crash path is the fallback
        traceback.print_exc(file=sys.stderr)
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)


# ---------------------------------------------------------------------------
# Case execution
# ---------------------------------------------------------------------------
def run_case(case: dict, result_queue, control_queue=None) -> None:
    """Child entry point: execute `case`, post `done`, exit. Does not return.

    `case` keys: case_id, mode ("throughput"/"accuracy"), model_dir, device,
    context_tokens, output_tokens, warmup, iterations, iteration_gap_sec, timeout_sec,
    ov_config, scheduler_config, mtp, throughput_task, probes.
    """
    channel = _Channel(case.get("case_id"), result_queue, control_queue,
                       case.get("timeout_sec") or 600)
    try:
        done = execute_case(case, channel)
    except BaseException as exc:  # noqa: BLE001 - always report, whatever happened
        done = {
            "event": "done", "case_id": case.get("case_id"), "load_ok": False,
            "iterations": [], "probes": [], "stage_reached": STAGE_START,
            "error": _error(exc, _Progress(), "error"),
        }
    # No `del pipe` / gc.collect() anywhere: see _post_and_exit().
    _post_and_exit(result_queue, done)


def execute_case(case: dict, channel: _Channel) -> dict:
    """Load once, run the case's generations and return the terminal `done` message."""
    ov_config = dict(case.get("ov_config") or {})
    scheduler_config = dict(case.get("scheduler_config") or {})
    mtp = dict(case.get("mtp") or {})
    model_dir, device = case["model_dir"], case["device"]
    progress = _Progress()
    done = {
        "event": "done",
        "case_id": case.get("case_id"),
        "context_tokens": case.get("context_tokens"),
        "load_ok": False,
        "load_time_s": None,
        "prompt": None,
        "prompt_tokens": None,
        "iterations": [],
        "probes": [],
        "stage_reached": STAGE_START,
        "phase": "load",
        "active_iteration": None,
        "error": None,
    }

    diagnostics = _device_diagnostics(device, ov_config)
    if diagnostics["unsupported_properties"]:
        print(
            f"[trial_runner] {device} does not advertise: "
            f"{', '.join(diagnostics['unsupported_properties'])} -- passed unchanged; a "
            "runtime rejection is reported as unsupported",
            file=sys.stderr,
        )
    done["device"] = diagnostics

    try:
        channel.post("device", **diagnostics)
        t0 = time.perf_counter()
        tokenizer = _load_tokenizer(model_dir)
        pipe, accepts_tokenized_input = _load_pipeline(
            model_dir, device, ov_config, scheduler_config, mtp
        )
        done["load_ok"] = True
        done["load_time_s"] = time.perf_counter() - t0
        progress.milestone, progress.phase = STAGE_LOADED, "prompt"
        channel.post("loaded", load_time_s=done["load_time_s"])
    except Exception as exc:  # noqa: BLE001 - reported to the parent, not re-raised
        _log_failure("load")
        done["error"] = _error(exc, progress)
        return _finish(done, progress)

    try:
        if case.get("mode") == "accuracy":
            _run_accuracy_probes(pipe, accepts_tokenized_input, tokenizer, channel, done,
                                 progress, case, mtp)
        else:
            _run_throughput_iterations(pipe, accepts_tokenized_input, tokenizer, channel, done,
                                       progress, case, mtp)
    except Exception as exc:  # noqa: BLE001
        _log_failure(failing_stage(progress.milestone))
        done["error"] = _error(exc, progress)
    return _finish(done, progress)


def _finish(done: dict, progress: _Progress) -> dict:
    done["stage_reached"] = progress.milestone
    done["phase"] = progress.phase
    done["active_iteration"] = progress.active_iteration
    return done


def _generate_once(pipe, pipeline_input, channel, progress, index: int, warmup: bool,
                   max_new_tokens: int, prompt_info: dict, mtp: dict, case_id) -> dict:
    """One timed generate with fresh request state; returns its iteration record."""
    import openvino_genai as ov_genai

    gen_config = generation_config(max_new_tokens, mtp)
    ttft_ms = None
    t1 = time.perf_counter()

    def _on_token(_: str):
        """First call = prefill is over. Records only *when*; never stops generation."""
        nonlocal ttft_ms
        if ttft_ms is None:
            ttft_ms = (time.perf_counter() - t1) * 1000.0
            progress.milestone = STAGE_PREFILLED
            channel.post("prefilled", iteration=index)
        return ov_genai.StreamingStatus.RUNNING

    progress.phase = "generate"
    result = pipe.generate(pipeline_input, generation_config=gen_config, streamer=_on_token)
    wall_seconds = time.perf_counter() - t1
    progress.milestone = STAGE_DECODED
    progress.phase = "record"

    perf = read_perf_metrics(result)
    runtime_output = perf.get("output_size")
    output_size = runtime_output or generated_token_count(result, pipe.get_tokenizer())
    if not output_size:
        raise RuntimeError("no_output: generate() produced no tokens")
    output_text = generated_text(result, pipe.get_tokenizer())

    prompt_tokens = prompt_info["ov_tokens"]
    runtime_input = perf.get("input_size")
    input_size = runtime_input or prompt_tokens
    values, sources = metrics.select_timing(perf, wall_seconds, ttft_ms, output_size)
    sources["input_size"] = metrics.SOURCE_RUNTIME if runtime_input else metrics.SOURCE_PROMPT
    sources["output_size"] = metrics.SOURCE_RUNTIME if runtime_output else "tokenizer"
    enabled = bool(mtp.get("enabled"))
    record = metrics.iteration_record(
        iteration=index,
        input_size=input_size,
        output_size=output_size,
        warmup=warmup,
        num_assistant_tokens=mtp.get("num_assistant_tokens") if enabled else None,
        verification_steps=perf.get("verification_steps"),
        mtp_acceptance_rate=perf.get("mtp_acceptance_rate"),
        mtp_draft_tokens=perf.get("mtp_draft_tokens"),
        mtp_accepted_tokens=perf.get("mtp_accepted_tokens"),
        mtp_rejected_tokens=perf.get("mtp_rejected_tokens"),
        mtp_draft_to_main_ratio=perf.get("mtp_draft_to_main_ratio"),
        output_sha256=_sha256(output_text),
        sources=sources,
        **values,
        **{field: perf.get(field) for field in PASS_PROFILE_FIELDS},
    )
    record.update({
        "case_id": case_id,
        "status": "ok",
        "speculative_strategy": mtp.get("strategy") if enabled else None,
        "max_new_tokens": max_new_tokens,
        "prompt_sha256": prompt_info["prompt_sha256"],
        "prompt_tokens": prompt_tokens,
        # A runtime count that differs from the counted input invalidates exact-context
        # certification; the timings are kept for diagnosis.
        "input_count_mismatch": bool(runtime_input and runtime_input != prompt_tokens),
        # GenAI does not expose a finish reason here, so reaching the ceiling is reported as
        # potential truncation rather than guessed either way.
        "finish_reason": None,
        "potentially_truncated": output_size >= max_new_tokens,
        "wall_generation_time": wall_seconds,
        "wall_first_token_latency": ttft_ms,
        "output_text": output_text,
    })
    return record


def _run_throughput_iterations(pipe, accepts_tokenized_input, tokenizer, channel, done,
                               progress, case, mtp) -> None:
    """One prompt built once, then ``warmup + iterations`` generations over it."""
    context_tokens = case["context_tokens"]
    output_tokens = case["output_tokens"]
    warmup, iterations = case["warmup"], case["iterations"]
    gap = float(case.get("iteration_gap_sec") or 0.0)

    progress.milestone, progress.phase = STAGE_LOADED, "prompt"
    t0 = time.perf_counter()
    prompt, hf_tokens = build_benchmark_prompt(tokenizer, context_tokens,
                                               case.get("throughput_task"))
    pipeline_input, prompt_tokens = prepare_pipeline_input(pipe, prompt, accepts_tokenized_input)
    # Context 0 is a standalone prompt of its natural length, so only the two tokenizers
    # have to agree; otherwise all three counts must equal the configured context.
    expected = context_tokens or hf_tokens
    if hf_tokens != expected or prompt_tokens != expected:
        raise PromptSizeError(
            "Prompt token count mismatch: "
            f"requested={context_tokens}, huggingface={hf_tokens}, openvino={prompt_tokens}"
        )
    prompt_info = {
        "configured_tokens": context_tokens,
        "hf_tokens": hf_tokens,
        "ov_tokens": prompt_tokens,
        "prompt_sha256": _sha256(prompt),
        "build_time_s": time.perf_counter() - t0,
    }
    done["prompt"] = prompt_info
    done["prompt_tokens"] = prompt_tokens
    progress.milestone = STAGE_PROMPT_BUILT
    channel.post("prompt", **prompt_info)

    for index in range(warmup + iterations):
        is_warmup = index < warmup
        progress.milestone, progress.active_iteration = STAGE_PROMPT_BUILT, index
        # Before `iteration_start`, so neither the timer nor the parent's memory window
        # includes the idle; never before a warm-up.
        if not is_warmup and gap > 0:
            progress.phase = "idle_gap"
            time.sleep(gap)
        progress.phase = "ack"
        channel.post("iteration_start", iteration=index, warmup=is_warmup)
        channel.wait_ack(index, ACK_READY)
        # Warm-up uses the full decode ceiling, like the measured iterations.
        record = _generate_once(pipe, pipeline_input, channel, progress, index, is_warmup,
                                output_tokens, prompt_info, mtp, case.get("case_id"))
        done["iterations"].append(record)
        progress.phase = "ack"
        channel.post("iteration", **record)
        channel.wait_ack(index, ACK_RECORDED)
    progress.active_iteration = None
    progress.phase = "done"


def _run_accuracy_probes(pipe, accepts_tokenized_input, tokenizer, channel, done,
                         progress, case, mtp) -> None:
    """One prompt and decode budget per probe; warm-up on the first buildable probe.

    A prompt-build failure produces an explicit skipped-probe record and processing
    continues; an inference failure ends the case. `benchmark.iterations` does not multiply
    probes. Answers go back to the parent for scoring; nothing is scored here.
    """
    context_tokens = case["context_tokens"]
    warmup = case["warmup"]
    gap = float(case.get("iteration_gap_sec") or 0.0)
    probes = list(case.get("probes") or [])
    built: dict = {}

    def _build(spec: dict) -> tuple:
        progress.milestone, progress.phase = STAGE_LOADED, "prompt"
        t0 = time.perf_counter()
        prompt, hf_tokens = build_probe_prompt(
            tokenizer, context_tokens, [tuple(pair) for pair in spec["inserts"]],
            spec["question"], spec.get("filler"), tolerance=PROBE_TOKEN_TOLERANCE,
        )
        if spec.get("truths") and not word_list_ranking_holds(
                prompt, spec["question"], spec["truths"]):
            # Skipped, not scored: the expected answer is no longer the right one.
            raise PromptSizeError(
                "the word list cut to this context no longer ranks the expected words first"
            )
        pipeline_input, prompt_tokens = prepare_pipeline_input(
            pipe, prompt, accepts_tokenized_input
        )
        for source, count in (("huggingface", hf_tokens), ("openvino", prompt_tokens)):
            if abs(count - context_tokens) > PROBE_TOKEN_TOLERANCE:
                raise PromptSizeError(
                    f"Probe prompt token count mismatch: requested={context_tokens}, "
                    f"{source}={count}, tolerance={PROBE_TOKEN_TOLERANCE}"
                )
        progress.milestone = STAGE_PROMPT_BUILT
        return pipeline_input, {
            "configured_tokens": context_tokens,
            "hf_tokens": hf_tokens,
            "ov_tokens": prompt_tokens,
            "prompt_sha256": _sha256(prompt),
            "build_time_s": time.perf_counter() - t0,
        }

    def _coordinates(spec: dict, index: int) -> dict:
        return {
            "iteration": index,
            "probe_index": spec["probe_index"],
            "suite": spec.get("suite"),
            "task": spec.get("task"),
            "depth": spec.get("depth"),
            "sample": spec.get("sample"),
            "fixture_id": spec.get("fixture_id"),
        }

    def _skip(spec: dict, exc: Exception) -> None:
        index = warmup + spec["probe_index"]
        reason = sanitize_message(f"{type(exc).__name__}: {exc}")
        print(f"[trial_runner] skipping probe {spec.get('task')} #{spec.get('sample')}: {reason}",
              file=sys.stderr)
        record = {**_coordinates(spec, index), "case_id": case.get("case_id"),
                  "warmup": False, "status": "skipped", "skip_reason": reason}
        done["probes"].append(record)
        progress.phase = "ack"
        channel.post("probe", **record)
        channel.wait_ack(index, ACK_RECORDED)

    def _build_or_skip(spec: dict):
        try:
            return _build(spec)
        except ValueError as exc:
            _skip(spec, exc)
            return None

    # Warm-up on the first buildable probe, with the case-wide decode ceiling.
    warm = None
    if warmup:
        for spec in probes:
            warm = _build_or_skip(spec)
            if warm is not None:
                built[spec["probe_index"]] = warm
                channel.post("prompt", iteration=warmup + spec["probe_index"], **warm[1])
                break
        if warm is None:
            return  # nothing buildable: the parent reports no_output
        for index in range(warmup):
            progress.milestone, progress.active_iteration = STAGE_PROMPT_BUILT, index
            progress.phase = "ack"
            channel.post("iteration_start", iteration=index, warmup=True)
            channel.wait_ack(index, ACK_READY)
            record = _generate_once(pipe, warm[0], channel, progress, index, True,
                                    case["output_tokens"], warm[1], mtp, case.get("case_id"))
            record.pop("output_text", None)
            done["iterations"].append(record)
            progress.phase = "ack"
            channel.post("iteration", **record)
            channel.wait_ack(index, ACK_RECORDED)

    skipped = {r["probe_index"] for r in done["probes"] if r.get("status") == "skipped"}
    for spec in probes:
        if spec["probe_index"] in skipped:
            continue
        index = warmup + spec["probe_index"]
        progress.active_iteration = index
        prepared = built.pop(spec["probe_index"], None)
        if prepared is None:
            prepared = _build_or_skip(spec)
            if prepared is None:
                continue
            channel.post("prompt", iteration=index, **prepared[1])
        pipeline_input, prompt_info = prepared
        done["prompt_tokens"] = prompt_info["ov_tokens"]
        progress.milestone = STAGE_PROMPT_BUILT
        if gap > 0:
            progress.phase = "idle_gap"
            time.sleep(gap)
        progress.phase = "ack"
        channel.post("iteration_start", iteration=index, warmup=False)
        channel.wait_ack(index, ACK_READY)
        record = _generate_once(pipe, pipeline_input, channel, progress, index, False,
                                spec.get("output_tokens") or case["output_tokens"], prompt_info,
                                mtp, case.get("case_id"))
        prediction_text = record.pop("output_text")
        probe = {**record, **_coordinates(spec, index), "prediction_text": prediction_text}
        done["probes"].append(probe)
        progress.phase = "ack"
        channel.post("probe", **probe)
        channel.wait_ack(index, ACK_RECORDED)
    progress.active_iteration = None
    progress.phase = "done"

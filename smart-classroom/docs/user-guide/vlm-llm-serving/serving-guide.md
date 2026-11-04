# VLM/LLM Serving Guide

How to select a text-generation model, enable speculative decoding, prepare
models ahead of time, call tools through port 8000 and read processing-time
statistics. The requirements behind these features are in
[vlm-llm-serving-design.md](../../dev-guide/vlm-llm-serving/vlm-llm-serving-design.md);
the offline benchmark is described in the
[Context Bench guide](../../dev-guide/context-bench/context-bench-guide.md) and
[design](../../dev-guide/context-bench/context-bench-design.md).

> **Qualification status.** The model profiles below are implemented, but
> hardware qualification (the ARL/PTL matrix in the design) and tool-call
> qualification of each profile are still pending. Treat accelerated profiles
> as candidates until their results are published.

## Model Choice

The target is selected as before, under `models.text_gen` in
[config.yaml](../../../Windows/config.yaml). Existing selections keep working unchanged.

| `vlm_name` | `weight_format` | Optional accelerator |
| --- | --- | --- |
| `Qwen/Qwen3-VL-8B-Instruct` (default) | `int4`, `int8` | `eagle3` (`int4` only) |
| `Qwen/Qwen3.5-9B` | `int4`, `int8` | `dflash`, `mtp` (target-bundled) |
| `Qwen/Qwen3.6-35B-A3B` | `int4` | `dflash` |
| `Qwen/Qwen3.8-27B` | `int4`, `int8` | none (plain) |

Pre-converted OpenVINO IR is downloaded on first start into
`models/openvino/<model>/<weight_format>`. Other models keep the existing
export path. The catalog lives in
[components/vlm/text_gen_profiles.py](../../../Windows/components/vlm/text_gen_profiles.py).

## Speculative Decoding

Add an optional `speculative` block under `models.text_gen`. Omitting it, or
`mode: "off"`, is plain generation and prepares only the target.

```yaml
models:
  text_gen:
    provider: vlm
    vlm_name: Qwen/Qwen3.6-35B-A3B
    device: GPU
    weight_format: int4
    speculative:
      mode: auto          # "off" | auto | "on"
      method: auto        # auto | eagle3 | dflash | dflash2 | mtp
      draft_source: null  # null | huggingface | local | target (MTP)
      draft_model: null
      draft_revision: null
      num_assistant_tokens: null
```

With `method: auto` the catalog picks the profile of `vlm_name`: `eagle3` for
Qwen3-VL-8B-Instruct INT4 (only with `app.language: en`), `dflash` for Qwen3.5-9B
INT4/INT8 and Qwen3.6-35B-A3B INT4. Qwen3.5-9B with `draft_source: target` selects
its target-bundled `mtp` head.

Quote `"off"` and `"on"`: bare `off`/`on` are YAML booleans (they are accepted,
but quoting avoids surprises).

| Setting | Behavior |
| --- | --- |
| `mode: auto` | Try the selected profile when its runtime and artifacts are available and it warms up; otherwise plain generation, with the reason in `/health`. This does not certify performance or accuracy. |
| `mode: "on"` | Acceleration is required at startup; any reason it cannot run is a startup failure with guidance. |
| `method` | `eagle3` (Qwen3-VL-8B INT4), target-bundled `mtp` (Qwen3.5-9B INT4/INT8), and `dflash` (Qwen3.5-9B INT4/INT8, Qwen3.6 INT4) have profiles; `auto` selects the one for `vlm_name`. `dflash2` has no profile yet: plain generation under `auto`, a startup failure under `"on"`. |
| `draft_source: target` | MTP only: use the target repository's `openvino_mtp_model.xml` and `.bin`; `draft_model` and `draft_revision` must be null. |
| `draft_source: local` | `draft_model` names an existing OpenVINO IR directory (relative paths resolve from `smart-classroom/Windows/`). It is validated, never downloaded or replaced. |
| `draft_revision` | A full 40-character commit. `null` uses the catalog pin (`29ffa460d80e…` for the Eagle3 drafter, `f181eece646a…` for the Qwen3.6 drafter, `5fc3b3d47476…` for the Qwen3.5-9B drafter). |
| `num_assistant_tokens` | Draft window `k`; `null` uses the profile default. Eagle3: 4, range 1–8. DFlash: 5 (best for classroom summaries and chat; 7 suits code/math), and 7 for Qwen3.5-9B INT8, range 1–15. Qwen3.5 MTP: 2, range 1–15. |

Invalid settings (unknown method or key, contradictory source, cross-model
drafter, out-of-range `k`) fail validation at startup under `auto` and `"on"`;
they never silently fall back. Under `"off"` the other fields are not read.

While a speculative pipeline is active:

* Requests are greedy. A request with `temperature > 0` is rejected with HTTP 400
  instead of being changed. Classroom stages already generate greedily; grading
  does not (`vlm.temperature: 0.2` in
  [components/grading/config.yaml](../../../Windows/components/grading/config.yaml)), so set
  that to 0 before using grading with speculative decoding. Sampling penalties
  are rejected the same way.
* Image requests run on the same pipeline (including Qwen3.5 MTP);
  image-encoding speedups are not claimed.
* Generation runs on the paged (continuous-batching) backend instead of the
  stateful one. Greedy outputs therefore differ in places from plain
  generation, with no consistent accuracy effect.
* If activation fails under `auto`, the server warms a plain pipeline once and
  keeps that decision until the process restarts.

### MTP for Qwen3.5-9B

The INT4 and INT8 OpenVINO target repositories include the MTP IR files
(`openvino_mtp_model.xml`/`.bin`). Enable the target-bundled head without
configuring a separate drafter:

```yaml
    vlm_name: Qwen/Qwen3.5-9B
    weight_format: int4
    speculative:
      mode: "on"
      method: mtp
      draft_source: target
      draft_model: null
      draft_revision: null
      num_assistant_tokens: null
```

MTP requires OpenVINO GenAI 2026.4.1 or later. The head is loaded from the
ordinary target directory into the same `VLMPipeline` used for plain
generation, so no derived model directory is created and image requests keep
working.

* **Profile defaults.** `num_assistant_tokens: 2` (range 1–15), a scheduler
  with `max_num_seqs: 1` and prefix caching off, and `GPU_QUEUE_PRIORITY`
  `MEDIUM` for the text-generation pipeline. As with Eagle3, the queue priority
  overrides `models.gpu_queue_priority` for text generation only.
* **Observed behavior.** In replayed English lessons, k=2 was the only window
  that kept up with plain generation, and MTP was still not a net gain: draft
  acceptance varies widely between calls, and the paged pipeline lengthens the
  time to first token. Larger windows are slower, because most of the extra
  drafts are rejected. Compare MTP and plain at the same
  `models.gpu_queue_priority`: the profile's MEDIUM priority alone speeds up
  decoding.
* **Not yet qualified.** The profile has not been performance- or
  accuracy-qualified on the Arrow Lake/Panther Lake matrix or on Chinese
  lessons, and `context_bench` does not yet benchmark MTP. Keep `mode: "off"`
  unless your own workload shows a gain over plain at the same queue priority.

Each model call's `decoding`, `draft_tokens`, `accepted_tokens` and
`acceptance_rate` are recorded in `<session>/logs/model_calls.jsonl`. The draft
and acceptance counts are reported by the runtime (`extended_perf_metrics`) and
confirm that MTP is active.

### DFlash for Qwen3.5-9B

The drafter is
[z-lab/Qwen3.5-9B-DFlash](https://huggingface.co/z-lab/Qwen3.5-9B-DFlash)
(Apache-2.0, about 1B parameters) at a pinned commit, exported to INT4 with the
same recipe as the Qwen3.6 drafter. It reads the hidden states of target layers
1, 5, …, 29; the OpenVINO INT4 and INT8 target IR already name them
(`hidden_states_decoder_layers`), so the ordinary target directory is used and no
derived target is created. The one drafter export serves both precisions; each
target precision gets its own copy with the target's lm_head (`int4-lmhead` for
INT4, `int4-lmhead-int8` for INT8). Enable it with:

```yaml
    vlm_name: Qwen/Qwen3.5-9B
    weight_format: int4
    speculative:
      mode: auto
      method: auto
```

`method: auto` selects DFlash for Qwen3.5-9B INT4 and INT8. To use the
target-bundled MTP head instead, set `method: mtp` (or `draft_source: target`).

* **Profile defaults.** `num_assistant_tokens: 5` for INT4 and 7 for INT8 (range 1–15), OpenVINO GenAI
  2026.4.1 or later, `GPU_QUEUE_PRIORITY` `MEDIUM`, the runtime's default
  KV-cache precision, and the Qwen3.6 DFlash scheduler (`max_num_seqs: 1`,
  `num_linear_attention_blocks: 17`, `max_num_batched_tokens: 65536`, prefix
  caching off).
* **First start.** The drafter is downloaded and exported once; later starts
  reuse it. The second precision reuses the export and only builds its lm_head
  copy.
* **Window per precision.** On INT8 a verification pass costs more relative to
  a draft pass, so the longer window k=7 is the INT8 default; INT4 keeps k=5.
  Unlike Eagle3, the drafter also helps Chinese lessons. Text, JSON-schema and
  image requests pass through the warm pipeline on both precisions.
* **Not yet qualified.** The profile has been measured only offline, not
  through a replayed lesson, and accuracy has not been measured beyond output
  spot checks.

### Eagle3 for Qwen3-VL-8B-Instruct

The drafter is
[AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3](https://huggingface.co/AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3)
(MIT) at a pinned commit, exported to INT4 (259 MB). The target model, its
precision and its IR are unchanged. Enable it with:

```yaml
    vlm_name: Qwen/Qwen3-VL-8B-Instruct
    weight_format: int4
    speculative:
      mode: auto
      method: auto
```

* **English deployments only (`app.language: en`).** The drafter was trained
  on English data. On Chinese classroom summaries it is slower than plain (as
  is the `taobao-mnn/Qwen3-VL-8B-Instruct-Eagle3` drafter): the drafts are
  rejected too often to pay for themselves. The paged pipeline that
  speculative decoding requires also lengthens the time to first token of long
  Chinese summary prompts. With `app.language: zh` the profile is
  therefore not used: `mode: auto` loads the ordinary plain pipeline (no
  drafter is downloaded or loaded; `/health` gives the reason) and
  `mode: "on"` fails at startup.
* **GPU queue priority.** Each draft/verify step submits many small GPU jobs,
  and on a LOW-priority queue they wait long enough to cancel the gain. The
  profile therefore runs its pipeline at `GPU_QUEUE_PRIORITY` `MEDIUM`,
  overriding `models.gpu_queue_priority` for text generation only; other
  services keep the configured priority. The KV cache keeps the runtime's
  default precision (an f16 cache made long classroom prompts slower).

To roll back, remove the block (or set `mode: "off"`) and restart. Prepared
drafter files are kept for later use; superseded ones are moved aside, never
deleted. Preparation of the drafter is described in
[Model Preparation](#model-preparation).

## Model Preparation

No model files need to exist beforehand. The backend prepares the configured
model when it loads the warm VLM at startup, so
[start-smart-classroom.ps1](../../../Windows/start-smart-classroom.ps1), the desktop app
and `python main.py` all prepare the same way, with no extra switch. Code:
[text_gen_prepare.py](../../../Windows/components/vlm/text_gen_prepare.py) (preparation)
and [text_gen_vlm.py](../../../Windows/components/vlm/text_gen_vlm.py) (load and warm-up).

### First start

1. **Validate the configuration.** Invalid `models.text_gen` or `speculative`
   settings fail before any download.
2. **Target.** A catalog model downloads its pre-converted OpenVINO IR
   (`OpenVINO/<model>-<weight_format>-ov`) into
   `models/openvino/<model>/<weight_format>`. The download resumes where it
   stopped and is retried 3 times. Any other model is exported locally with
   optimum-intel, as before. A target that cannot be prepared fails startup in
   every mode.
3. **Drafter** (only when speculative decoding is requested and qualified):
   1. Its source checkpoint is downloaded at the catalog's pinned commit:
      configuration and weight files only, never repository code. Free disk
      space is checked first (2.2x the checkpoint).
   2. It is exported to int4 IR in a separate process that works offline from
      the downloaded files and does not use `--trust-remote-code`.
      * DFlash uses the DFlash classes built into optimum-intel 2.2.0
        (installed by `requirements.txt`).
      * Eagle3 uses optimum-intel's Eagle3 classes, whose VLM export path
        needs transformers 4.57.6. That version (with huggingface_hub 0.36.0)
        is installed once with `pip --no-deps --target` into
        `models/.export-deps/transformers-4.57.6` (about 110 MB) and used only
        by the export process. The
        checkpoint's weights are re-keyed in a temporary copy; the downloaded
        snapshot is not modified.
   3. The pair is checked.
      * DFlash: drafter metadata, vocabulary size, the target layers it reads,
        and `k` against its block size.
      * Eagle3: the drafter is an Eagle3 model for a `qwen3_vl` target, its
        hidden and vocabulary sizes match the target, and the IR has the
        Eagle3 inputs and outputs (`hidden_states`, `inputs_embeds`, 3-D
        `position_ids`, `logits`, `d2t`, `eagle3_mode`).
   4. DFlash only: the target's lm_head is attached to a copy of the drafter
      (below). Eagle3 keeps its own draft-vocabulary head and uses the
      ordinary target IR.

   The first accelerated start takes a few minutes: the drafter download,
   any export dependencies and the export.
4. **Manifest.** `models/openvino/.manifests/<model>-<weight_format>.json`
   records what was prepared, the drafter commit, the decision and timings.
5. **Load and warm up.** The pipeline (with the drafter, if any) is compiled on
   the device. Tool support is read from the chat template, and one 2-token
   greedy generation runs. The backend accepts requests and reports `/health`
   only after warm-up, and the start script waits for it.

| Artifact | Location |
| --- | --- |
| Target IR | `models/openvino/<model>/<weight_format>` |
| Drafter source (pinned commit) | `models/hf/<org>__<repo>/<commit>` |
| Drafter IR | `models/openvino/<drafter>/int4` (`.prepared.json` records the commit), e.g. `Qwen3-VL-8B-Instruct-eagle3/int4` |
| Eagle3 export dependencies | `models/.export-deps/transformers-4.57.6` (export only; never imported by the server) |
| DFlash drafter with its own lm_head (the one loaded) | `models/openvino/<drafter>/int4-lmhead`. The target's int8 lm_head is requantized to int4 and attached to the drafter, so the runtime stops reading the full int8 head on every draft pass (2-7% cheaper verification passes). Not built for `draft_source: local`. |
| DFlash target with hidden-state metadata | `models/openvino/<model>/<weight_format>-dflash`. Only when the stock export lacks `hidden_states_decoder_layers`; weights are hard-linked and the ordinary target is untouched. |
| Manifest | `models/openvino/.manifests/<model>-<weight_format>.json` |

### Later starts

Prepared files are validated and reused, without network access:

| Artifact | Reused when |
| --- | --- |
| Target | Its IR, tokenizer and detokenizer are present. |
| Drafter | Its IR is complete and its recorded commit matches the pin. |
| Derived artifacts | The SHA-256 stamp of the IR they were built from still matches. |

A stale artifact is rebuilt and the old copy is moved aside with a timestamp
suffix, never deleted. The drafter and derived artifacts are built in a
`.partial` directory and published only when complete, and the target download
resumes, so an interrupted first start recovers on the next one.
A `.lock` file per artifact keeps two starts from building it at once (a lock
left by a dead process is recovered). The second start then reuses the result.

The tokenizer is also checked against the installed openvino-tokenizers: an IR
exported by a newer release fails with an upgrade hint instead of a native
crash.

### Failures and offline use

| Failure | `mode: auto` | `mode: "on"` |
| --- | --- | --- |
| Target download or export (also in plain mode) | Startup fails | Startup fails |
| Drafter download, export, or a runtime older than the profile needs | Plain generation; reason in `/health` and the manifest | Startup fails with guidance |
| Drafter incompatible with the target | Startup fails | Startup fails |
| Pipeline load or warm-up with the drafter | Plain pipeline, decision kept until restart | Startup fails |
| Drafter lm_head cannot be built | Stock drafter is loaded (warning) | Stock drafter is loaded (warning) |

To run offline, start once with network access, or copy the prepared
`models/` directories (and `models/hf/` for a drafter) to the machine.

`/health` reports where startup time went (`startup.prepare_s`, `load_s`,
`warmup_s`). Warm-up covers text generation only: the first request with an
image still initializes the vision encoder.

## Tool Calling on Port 8000

`POST /v1/chat/completions` accepts OpenAI function tools on tool-capable
models (the Qwen3/3.5/3.6/3.8 templates render tools). The server returns calls;
**it never executes them** — run, authorize and bound tool loops in your client.

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused")
tools = [{"type": "function", "function": {
    "name": "get_weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}]
messages = [{"role": "user", "content": "Weather in Paris?"}]

first = client.chat.completions.create(model="text_gen", messages=messages, tools=tools)
call = first.choices[0].message.tool_calls[0]          # finish_reason == "tool_calls"
messages += [first.choices[0].message,
             {"role": "tool", "tool_call_id": call.id, "content": '{"temp_c": 21}'}]
answer = client.chat.completions.create(model="text_gen", messages=messages, tools=tools)
```

Any OpenAI-compatible client or agent framework that takes a base URL works
the same way. A request uses the tool path when it has `tools` or
`tool_choice`, or its history contains tool calls or results.

| Field | Supported |
| --- | --- |
| `tools` | `type: "function"` with `name`, optional `description`, JSON Schema `parameters` (`type`, `properties`, `required`, `items`, `enum`, `const`, `additionalProperties`, numeric/length/item bounds, `anyOf`/`oneOf`/`allOf`, `nullable`, annotations). `$ref` and `pattern` are rejected with HTTP 400. |
| `tool_choice` | `"none"`, `"auto"` (default with tools), `"required"`, or `{"type": "function", "function": {"name": ...}}`. |
| `parallel_tool_calls` | `false` returns at most one call; later calls in the same turn are dropped unparsed. |
| History | System/developer, user (text and `data:image/` images), assistant (with `tool_calls`, content may be null) and `tool` messages, in order. Every call must be answered by exactly one result before the conversation continues. Returned assistant messages can be sent back as they are, including `reasoning_content`. |
| `max_completion_tokens` | Also accepted as `max_tokens`. Omitted, the configured `max_new_tokens` applies. Leave room for reasoning: a thinking model reasons before it calls. |
| `enable_thinking` | Extra field: `false` skips reasoning on Qwen3.5/3.6/3.8 (faster calls); omitted, the model default applies. |

How the model's output is returned:

* **Calls.** The model's call syntax is normalized to `tool_calls`: JSON in
  `<tool_call>` for Qwen3-VL, `<function=...>` XML for Qwen3.5 and later. Each
  call gets a unique `id`, and arguments are a JSON-encoded object.
* **Reasoning.** A thinking model's reasoning is returned in
  `message.reasoning_content` (streaming: `delta.reasoning_content`), never in
  `content`. Calls written inside reasoning are ignored.
* **Forced calls.** `"required"` and a named choice are enforced: the
  assistant turn is started with the opening of a call (and, for a thinking
  model, without reasoning), so the model cannot answer in text instead.
  `"auto"` leaves the decision to the model.
* **Validation.** Calls are validated against the offered schema. An invalid
  call, or a call to a function that was not offered, returns HTTP 502
  (`tool_call_validation_failed`) and is never returned as a call. A call cut
  off by the token limit returns `finish_reason: "length"` without calls.
* **Streaming.** Content and reasoning stream as they are generated. Calls are
  held until the turn is complete, then each is published whole in one
  `delta.tool_calls` chunk before `finish_reason: "tool_calls"`. A failure
  mid-stream ends with an `error` event and `[DONE]`.

Unsupported input is rejected with HTTP 400 before generation. That covers
malformed definitions or history, a choice naming an absent tool, a model
whose template does not render tools (`"tool_calls": false` in `/health`),
and `temperature > 0` while speculative decoding is active. Images given by URL or file path
are also rejected: tool conversations carry untrusted content, so the server
neither fetches URLs nor reads local files for them.

Requests without tools or tool messages use the existing chat path unchanged
(the last user turn is answered).

Verified end to end on Arc 140T (Arrow Lake) with the `openai` client:

| Profile | Result |
| --- | --- |
| Qwen3-VL-8B INT4 | 10/10 |
| Qwen3.5-9B INT4 | 10/10 |
| Qwen3.6-35B-A3B INT4 + DFlash | 10/10 |
| Qwen3.8-27B INT4 | 10/10 |
| Qwen3-VL-8B INT4 + Eagle3 | 3/3 (call and result round trip, streaming call, `required` choice; without reasoning) |

Each run covered, with and without reasoning:
- a call followed by a tool result and the final answer;
- streaming calls;
- two parallel calls, and `parallel_tool_calls: false`;
- named and `required` choices;
- a question that needs no tool.

This is a functional check, not the qualification matrix in the design.

## Health

`GET /health` keeps its existing fields and adds `hub.text_gen.serving` once the
model is loaded:

```json
{
  "model": "Qwen/Qwen3.6-35B-A3B", "weight_format": "int4", "device": "GPU",
  "tool_calls": true,
  "speculative": {"requested_mode": "auto", "requested_method": "auto",
                  "effective_method": "dflash", "draft": "z-lab/Qwen3.6-35B-A3B-DFlash@0123456789ab",
                  "num_assistant_tokens": 5, "fallback_reason": null},
  "warmup": {"status": "ok", "seconds": 1.8},
  "startup": {"prepare_s": 0.4, "load_s": 61.2, "warmup_s": 1.8},
  "diagnostics": {"openvino_genai": "2026.4.1.0", "draft_revision": "...", "scheduler": {}}
}
```

Local paths are never reported; a local drafter appears as `local IR (<dir name>)`.

## Processing-Time Statistics

`GET /processing-stats` reports workflow-stage and per-model-call timing:
queue wait, latency, TTFT, decode throughput and speculative (Eagle3/DFlash) acceptance, attributed
to the stage that made each call. Each session's records are also written to
its `logs/` folder, and `Scripts/processing_stats_report.py` turns them into a
Markdown report with a timeline. See
[processing-time-statistics.md](../../dev-guide/processing-time-statistics/processing-time-statistics.md).

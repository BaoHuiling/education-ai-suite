# Smart Classroom Vision-Language Model (VLM) and Large Language Model (LLM) Serving Design

**Status:** Implementation design. The requirements below are the contract; paragraphs marked **Implemented** describe how the current code meets them, and the table under Section 1 summarizes coverage. Hardware qualification (Section 8) and published performance results remain pending.

## 1. Key Features and Compatibility

This design covers shared model serving, speculative decoding, timing instrumentation, and offline benchmarking while preserving classroom inference interfaces.

| Key Feature | Stakeholder Benefit |
| --- | --- |
| **Qwen3.5, Qwen3.6, or Qwen3.8 model choice** | Serve Qwen3.5-9B INT4 and INT8, Qwen3.6-35B-A3B INT4, and Qwen3.8-27B INT4 and INT8 through the shared OpenVINO service. |
| **Tool calling on port 8000** | OpenAI-compatible function calls and tool-result conversations through `/v1/chat/completions`, enabling agent workflows without a separate endpoint. |
| **Selectable speculative decoding** | Choose Eagle3, DFlash, DFlash2 or supported MTP explicitly, with plain-generation fallback under the `auto` activation policy. |
| **Automatic first-run preparation** | Download the target and required speculative artifacts, convert them when needed, and reuse validated files on later starts. No pre-existing local model files are required. |
| **Backward compatibility** | Retain older models and configuration, the port-8000 API, `/v1/chat/completions`, and the shared classroom generation interface. |
| **Controlled activation** | Complete the warm-up of the selected model pipeline before reporting readiness, show the model's acceleration status, and handle failures safely. |
| **Workflow-stage and model-call timing statistics** | Measure processing time separately for workflow stages, including Automatic Speech Recognition (ASR), video analytics (VA), Optical Character Recognition (OCR), Summary, and Mindmap. Compare individual model calls by model, precision, device, and effective decoding method. |
| **Standalone `context_bench` benchmarking** | Compare model and runtime profiles using repeatable measurements of time to first token (TTFT), time per output token after the first (TPOT), throughput, and memory. Optionally assess retrieval accuracy and generation fidelity against a baseline. See [context-bench-design.md](../context-bench/context-bench-design.md). |

**Preserve existing model support**, including Qwen3-VL-8B-Instruct and Qwen3.5-9B, with unchanged providers, model IDs, precisions, local paths and generation behavior. No configuration migration or default model replacement is required; speculation remains switched off unless enabled. A model without a qualified accelerator continues through plain generation. Qualified serving profiles prepare missing artifacts automatically after software setup; the offline benchmark consumes prepared artifacts.

Keep one target per serving instance. Generating tool calls and consuming tool results are in scope; executing tools remains the client's or controller's responsibility. Multi-model routing, UI redesign, and NPU text-generation support are outside the scope; existing NPU use by other classroom components is unchanged.

**Implemented** (usage: [serving-guide.md](../../user-guide/vlm-llm-serving/serving-guide.md)):

| Feature | Current implementation |
| --- | --- |
| Model choice | Catalog in [text_gen_profiles.py](../../../Windows/components/vlm/text_gen_profiles.py): Qwen3-VL-8B-Instruct (default) and Qwen3.5-9B INT4/INT8, Qwen3.6-35B-A3B INT4, Qwen3.8-27B INT4/INT8, all declared tool-capable. Models outside the catalog keep the existing export path. |
| Speculative decoding | Eagle3 for Qwen3-VL-8B-Instruct INT4, DFlash for Qwen3.6-35B-A3B INT4, and for Qwen3.5-9B INT4/INT8 DFlash (the `auto` choice) or target-bundled MTP (with `method: mtp` or `draft_source: target`), both OpenVINO GenAI 2026.4.1 or later and not yet performance- or accuracy-qualified. `dflash2` is an accepted setting without a profile: plain generation under `auto`, a startup failure under `"on"`. |
| First-run preparation | [text_gen_prepare.py](../../../Windows/components/vlm/text_gen_prepare.py), run by the model owner at load (Section 5). |
| Tool calling | [tool_calls.py](../../../Windows/components/vlm/tool_calls.py) and [vlm_chat.py](../../../Windows/api/vlm_chat.py) (Section 3). |
| Controlled activation | Warm-up before readiness; `/health` reports `hub.text_gen.serving` (Section 6). |
| Timing statistics | Stage and model-call records, `GET /processing-stats`, per-session logs and a report script: [processing-time-statistics.md](../processing-time-statistics/processing-time-statistics.md) (Section 7). |
| `context_bench` | [components/context_bench](../../../Windows/components/context_bench/): [context-bench-design.md](../context-bench/context-bench-design.md). |

## 2. Model Profiles

| Model | Target Artifacts | Optional Accelerator |
| --- | --- | --- |
| Previously supported VLM and LLM models | Preserve their supported download and export paths, and precisions | Plain generation; a drafter is not required |
| `Qwen/Qwen3-VL-8B-Instruct` | [INT4 IR](https://huggingface.co/OpenVINO/Qwen3-VL-8B-Instruct-int4-ov) or [INT8 IR](https://huggingface.co/OpenVINO/Qwen3-VL-8B-Instruct-int8-ov) | [Eagle3](https://huggingface.co/AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3) (INT4 only) |
| `Qwen/Qwen3.5-9B` | [INT4 intermediate representation (IR)](https://huggingface.co/OpenVINO/Qwen3.5-9B-int4-ov) or [INT8 IR](https://huggingface.co/OpenVINO/Qwen3.5-9B-int8-ov) | [DFlash](https://huggingface.co/z-lab/Qwen3.5-9B-DFlash), or target-bundled MTP (`openvino_mtp_model.xml`); performance/accuracy qualification pending |
| `Qwen/Qwen3.6-35B-A3B` | [INT4 IR](https://huggingface.co/OpenVINO/Qwen3.6-35B-A3B-int4-ov) | [DFlash](https://huggingface.co/z-lab/Qwen3.6-35B-A3B-DFlash) |
| `Qwen/Qwen3.8-27B` | [INT4 IR](https://huggingface.co/OpenVINO/Qwen3.8-27B-int4-ov) or [INT8 IR](https://huggingface.co/OpenVINO/Qwen3.8-27B-int8-ov) | [DFlash2](https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2) (planned: no profile yet, so plain generation) |

A Qwen3.5-9B selection uses `provider: vlm`, `vlm_name: Qwen/Qwen3.5-9B`, and `weight_format: int4` or `int8` under `models.text_gen`. Omit `speculative` for plain generation. With `speculative.mode` `auto` or `"on"`, the catalog selects DFlash, or the target-bundled MTP head with `method: mtp` or `draft_source: target`; GenAI 2026.4.1 loads either into the same `VLMPipeline`, so image requests keep working. DFlash passed GPU smoke tests on INT4 and INT8, MTP on INT4 only; neither is yet performance- or accuracy-qualified.

A maintained catalog supplies compatible combinations of targets and methods, default drafter sources, immutable revisions, conversion recipes, dependency versions, and profile tuning values. Explicit speculative settings select or override these defaults only within a validated profile. Drafter precision is independent of target precision. Extend the catalog without restricting the existing plain-model loading path.

A source checkpoint is not runtime-ready IR. Each accelerated profile must provide downloadable compatible IR or a tested conversion recipe for downloaded source files, including target hidden-state outputs where needed. Qualify the DFlash2 exporter and runtime independently. Qwen3.5 MTP uses the target repository's `openvino_mtp_model.xml` and `.bin`; it does not use either external z-lab drafter. Select one mechanism per pipeline, never silently substitute or combine them.

**Implemented:** the Qwen3.6 DFlash profile pins drafter commit `f181eece646affea2c38b2765f1aaa01a9734ccd`, downloads only its configuration and weight files, and exports it to INT4 IR with the DFlash classes built into optimum-intel 2.2.0 (no repository code). The tested defaults are `num_assistant_tokens: 5` (range 1–15), OpenVINO GenAI 2026.3 or later, an f16 KV cache, and a scheduler with `max_num_seqs: 1`, `num_linear_attention_blocks: 17` and prefix caching off; the cache size is left to the runtime. When the stock target export lacks the hidden-state metadata the drafter needs, a derived target directory carries it (Section 5).

**Implemented:** the Qwen3-VL-8B Eagle3 profile pins drafter `AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3` at commit `29ffa460d80e1d88a015902ae0333ff87ffe3184` (MIT), downloads only its configuration and weight files, and exports it to INT4 IR (259 MB) with optimum-intel 2.2.0's Eagle3 classes under an export-only transformers 4.57.6 (no repository code). The ordinary target IR is used unchanged. The tested defaults are `num_assistant_tokens: 4` (range 1–8), OpenVINO GenAI 2026.4 or later, the runtime's default KV-cache precision, `GPU_QUEUE_PRIORITY: MEDIUM` for this pipeline (a LOW queue cancels the speedup), and a scheduler with `max_num_seqs: 1` and prefix caching off. The drafter predicts English only, so the profile is qualified for `app.language: en` only; with `zh`, `auto` keeps the plain pipeline and `"on"` fails at startup.

**Implemented:** the Qwen3.5-9B MTP profile (INT4 and INT8) uses the target repository's bundled `openvino_mtp_model.xml`/`.bin`; there is no separate drafter download or revision. OpenVINO GenAI 2026.4.1 or later loads the head into the same `VLMPipeline` as the target, so image requests keep working. The defaults are `num_assistant_tokens: 2` (range 1–15), `GPU_QUEUE_PRIORITY: MEDIUM` for this pipeline, the runtime's default KV-cache precision, and a scheduler with `max_num_seqs: 1` and prefix caching off. In replayed English lessons at equal queue priority, k=2 was the only window that kept up with plain generation, and MTP was still not a net gain.

## 3. Architecture

In the design requirements, serving has two phases: startup preparation and request handling.

![Startup and request flow for existing and new models](_assets/vlm-llm-serving-overview.svg)

Startup:
- A shared helper prepares model files once. The model owner, which can be the application, a supervised worker, or an independently operated server, loads and warms the model before serving classroom requests.
- Workers reuse prepared artifacts after restart.

Request Handling:
- Classroom workflows send requests to the ready model. The service limits how many requests it admits, can stream responses as they are generated, and supports cancellation.
- If a request is cancelled while native inference is still running, the service must keep that model capacity reserved until inference finishes.

Model upgrades do not require a new deployment topology. External clients do not download or manage the server’s model files. The server reports its actual model and acceleration state.

**Implemented:** the owner is the backend process itself. `ModelManager` warms `text_gen` at startup through `TextGenHandler`, which wraps one `VLMTextGen` pipeline in a `CapabilityRunner` (`concurrency` and `queue_max` from `models.text_gen`; a full queue returns HTTP 503). A streamed request holds its slot until native generation has stopped, including after a client disconnect, which cancels generation at the next token. A consumer that abandons a stream without closing it still releases the slot when the stream is garbage-collected.

### Tool Calls on Port 8000

**`POST /v1/chat/completions` on port 8000 must support OpenAI-compatible function (tool) calling**, in streaming and non-streaming requests. Qwen3.6 and Qwen3.8 serving profiles must pass the tool-call qualification; older tool-capable models use the same contract, while ordinary chat remains available for models without tool support. All serving ownership modes expose this contract through the same application endpoint.

| Contract | Required Behavior |
| --- | --- |
| Conversation | Preserve the full ordered `messages` history: system or developer, user, assistant, and tool turns, including supported image content. Do not reduce it to the last user prompt. Assistant messages may have null content when they contain tool calls. |
| Tool definitions | Accept request-level `tools` entries with `type: "function"`, function name, optional description, and a JSON Schema `parameters` object. Render definitions and history through the selected model's tool-aware chat template. |
| Tool selection | Support `tool_choice: "none"`, `"auto"`, `"required"`, or a named function object. Default to `auto` when tools are supplied, otherwise `none`. Reject choices referring to absent tools. Honor `parallel_tool_calls: false` by returning at most one call; multiple calls, when allowed, each require a distinct ID. |
| Tool response | Return `choices[].message.tool_calls` with stable `id`, `type: "function"`, `function.name`, and JSON-encoded `function.arguments`. Normalize model-specific tool syntax into this structure. Finish a valid tool-call turn with `finish_reason: "tool_calls"`; an ordinary completed text turn uses `"stop"`. |
| Tool results | The client appends the assistant tool-call message and a `role: "tool"` message with matching `tool_call_id` and result `content`, then submits the conversation again. Validate call-to-result associations and duplicate or missing results in the submitted history; server-side conversation storage is not required. |
| Streaming | Emit OpenAI-compatible Server-Sent Events (SSE) `delta.tool_calls` with stable call indexes and IDs. Buffer each generated call until its name and arguments are complete and validated, then publish it; finish with `finish_reason: "tool_calls"` and `[DONE]`. Do not expose partially generated arguments as a completed call. |

Tool-call sequence from the design requirements:

1. Request with tools: The client sends a request that includes tool definitions.
2. Assistant tool call: The assistant returns a tool call: a request to use one of those tools, with its name and arguments.
3. Client-authorized execution: Execution is client-authorized. The controller independently validates the arguments, authorizes execution, and bounds tool-loop iterations. The serving process does not execute a function, open a shell, or perform an action merely because the model requested it.
4. Tool result message: The client appends the assistant's tool-call message and a `role: "tool"` message containing the result and matching `tool_call_id`, then resubmits the conversation.
5. Assistant answer or next tool call: The assistant uses the result to answer, or returns another tool call and the sequence continues.

Tool-call validation and failure handling from the design requirements:

- Before invoking generation, the serving endpoint validates request schemas, tool names, selection options, and model capabilities.
- The serving endpoint returns a clear 4xx error for malformed requests or unsupported tool or schema features; it must not ignore them.
- The service endpoint validates each generated call against the offered function’s supported JSON Schema, not only its required keys.
- The service endpoint does not report an invalid, truncated, or missing required call as a successful tool invocation. It preserves failure and truncation semantics.
- If a stream has already started, the serving endpoint terminates it with an error rather than signaling a successful tool-call completion.
- The controller independently validates arguments, authorizes execution, and limits tool-loop iterations.
- The controller treats tool results as untrusted conversation data; it does not authorize further actions.

**Implemented** ([tool_calls.py](../../../Windows/components/vlm/tool_calls.py), [vlm_chat.py](../../../Windows/api/vlm_chat.py)):

- A request uses the tool path when it has `tools` or `tool_choice`, or its history contains tool calls or results; any other request keeps the existing chat path (last user turn).
- The supported JSON Schema subset covers `type`, `properties`, `required`, `items`, `enum`, `const`, `additionalProperties`, numeric, length and item bounds, `anyOf`/`oneOf`/`allOf`, `nullable` and annotations. Other keywords, such as `$ref` and `pattern`, are rejected with HTTP 400, as are malformed definitions, history or choices.
- Tool capability comes from the catalog and from the chat template rendering tools. A model without it rejects tool requests with HTTP 400; `tool_choice: "none"` still works.
- Qwen3 JSON calls and Qwen3.5+ `<function=...>` XML calls are both normalized. Calls inside reasoning are ignored, and reasoning is returned as `reasoning_content`, never in `content`.
- `"required"` and named choices are enforced: the assistant turn starts with the opening of a call in the model's syntax (reasoning closed first).
- An invalid call, or a call to a function not offered, returns HTTP 502 (`tool_call_validation_failed`). A token-limited turn returns `finish_reason: "length"` without calls, even when calls before the cut were complete.
- Streaming holds calls until the turn is complete. A rejected call or a generation failure after the stream starts ends it with an `error` event and `[DONE]`; generation failures report a generic message, with details only in the server log.
- Images must be inline `data:image/` URLs on both paths; the server never fetches a URL or reads a local file named by a request. `max_tokens` is accepted as an alias of `max_completion_tokens`, and both must be positive.

## 4. Configuration

- **Reuse the current configuration structure and add an optional `models.text_gen.speculative` block.**
- Keep the activation policy (`mode`) separate from the decoding mechanism (`method`) and from the draft model’s identity and source.
- Continue to select the target with `vlm_name` and `weight_format`.
- Do not rename existing keys or add top-level blocks.

The following example uses the structure of [config.yaml](../../../Windows/config.yaml), selects Qwen3.6 INT4 and enables DFlash, the implemented accelerated profile. It is an illustrative profile, not a snapshot of every installation's values. Preserve existing generation, admission, chunking and segmentation settings when merging it. Omitting `speculative` preserves plain generation. The shipped `config.yaml` keeps Qwen3-VL-8B-Instruct with `mode: "off"`. A DFlash2 selection (Qwen3.8 with `method: dflash2`, `draft_model: z-lab/Qwen3.8-27B-DFlash2`) is validated the same way, but runs plain under `auto` until its profile exists.

```yaml
models:
  text_gen:
    provider: vlm
    vlm_name: Qwen/Qwen3.6-35B-A3B
    device: GPU
    weight_format: int4
    max_new_tokens: 5120
    concurrency: 1
    queue_max: 8

    speculative:
      mode: auto
      method: dflash
      draft_source: huggingface
      draft_model: z-lab/Qwen3.6-35B-A3B-DFlash
      draft_revision: null
      num_assistant_tokens: null

    chunking:
      enabled: true
      max_prompt_tokens: auto
      max_content_tokens: 24000
      gpu_memory_safety_margin: 0.6
      map_max_new_tokens: 1536

    segmentation:
      window_max_new_tokens: 1024
      topics_target: 20
      topics_min: 15
      topics_max: 25
```

| Configuration Item | Required Behavior |
| --- | --- |
| Existing `provider`, `vlm_name`, `device`, `weight_format` | Keep names and meanings. For the new models use `provider: vlm`; select a listed model and precision combination. Keep an older installation's selection unless the operator changes it. |
| Existing `max_new_tokens`, `concurrency`, `queue_max` | Retain the installation's generation and admission limits; 5120, 1, and 8 are example values. A larger model does not automatically increase these limits. |
| Existing `chunking` and `segmentation` | Preserve the nested structure and configured values. Validate context and memory budgets for the selected model; change tuning only when measurements justify it. |
| New optional `speculative` block | Select activation policy, method, and draft artifacts without changing target or workflow settings. Defaults and validation are defined below. |

### Speculative Settings

| Field under `speculative` | Values and Default | Purpose |
| --- | --- | --- |
| `mode` | `"off"` (default), `auto`, `"on"` | Disable acceleration, allow plain fallback, or require acceleration. This is not the algorithm name. |
| `method` | `auto` (default), `eagle3`, `dflash`, `dflash2`, `mtp` | Select the decoding mechanism. `auto` selects the catalog's declared method, not trial-and-error loading. Future methods require an explicit supported profile. |
| `draft_source` | `null` (profile default), `huggingface`, `local`, `target` | Distinguish a Hub repository, an explicit local IR directory, and target-bundled MTP artifacts. Other sources require a supported adapter and profile. |
| `draft_model` | Repository ID or local directory; `null` selects the profile default | Identify the external drafter for DFlash or DFlash2. For MTP, set to null: use the target's prediction head, not a second model. |
| `draft_revision` | Immutable repository commit (a full 40-character hash); `null` selects the profile's pinned revision | Reproduce a Hub download. Reject non-null values for `local` or `target`; bundled MTP inherits the target revision. |
| `num_assistant_tokens` | Positive integer; `null` selects the profile default (Eagle3: 4, range 1–8; DFlash: 5, or 7 for Qwen3.5-9B INT8, range 1–15, and at most the drafter's block size minus one; Qwen3.5 MTP: 2, range 1–15) | Control the draft block size within the selected runtime's supported range. MTP's default is the best of k=2, 3 and 5 in single-lesson measurements, not a qualified setting. |

A profile may also be qualified for some `app.language` values only (Eagle3: `en`). Under another language it counts as unqualified: plain generation under `auto`, a startup failure under `"on"`.

| Selected Target and Method | Draft Settings |
| --- | --- |
| Qwen3-VL-8B INT4 with `eagle3` (implemented) | `draft_source: huggingface`, `draft_model: AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3` |
| Qwen3.6 INT4 with `dflash` (implemented) | `draft_source: huggingface`, `draft_model: z-lab/Qwen3.6-35B-A3B-DFlash` |
| Qwen3.8 INT4 or INT8 with `dflash2` (planned) | `draft_source: huggingface`, `draft_model: z-lab/Qwen3.8-27B-DFlash2` |
| Qwen3.5-9B INT4/INT8 with bundled `mtp` (implemented) | `draft_source: target`, `draft_model: null`; use the MTP files downloaded with the target |
| Qwen3.5-9B INT4/INT8 with `dflash` (implemented, the `auto` choice) | `method: auto` or `dflash`, `draft_source: huggingface`, `draft_model: z-lab/Qwen3.5-9B-DFlash` |

For example, replace only the speculative block with the following for Qwen3.5-9B:

```yaml
speculative:
  mode: auto
  method: mtp
  draft_source: target
  draft_model: null
  draft_revision: null
  num_assistant_tokens: null
```

Resolve and Validate:
- The helper resolves defaults before downloading, then validates the complete combination of target, method, source, and revision against artifact metadata and the supported profile.
- The model-serving software never changes an explicitly selected method if it fails; in `mode: auto`, the software falls back to the plain generation method.
- Unknown methods, contradictory sources, mismatched drafters, and out-of-range tuning fail the validation rather than triggering fallback. 
- In `"off"` mode, the helper ignores dormant speculative settings and prepares only the target.
- **Implemented:** `resolve_plan()` in [text_gen_profiles.py](../../../Windows/components/vlm/text_gen_profiles.py) applies these rules before any model work, so invalid settings under `auto` or `"on"` fail at startup. Bare YAML `off`/`on` booleans are accepted for `mode`.

Prepare Artifacts:
- The helper uses the catalog’s pinned Hub sources for first-run downloads. `draft_source: local` requires a non-empty `draft_model` directory and a null `draft_revision`; the helper validates the existing IR and does not download a replacement if it is missing. 
- The helper resolves relative directory paths from the Smart Classroom application root (`smart-classroom/Windows/`).
- `draft_source: target` is valid only with MTP, and requires `draft_model` and `draft_revision` to be set to null.
- The startup preparation flow ensures that a source override or revision cannot bypass compatibility checks, enable arbitrary URLs or execute repository code.
- The manifest records resolved settings and actual artifact versions, including defaults.
- Qwen3.5 MTP validates the target's bundled `openvino_mtp_model.xml`/`.bin` and loads them from the ordinary target directory; no derived copy is created.

Tool Calls:
Tool definitions and selection are per-request API fields (`tools`, `tool_choice`, and `parallel_tool_calls`), not new YAML settings or a separate service. Record tool and template capability in the model profile so unsupported requests fail before streaming; no `enable_tool_call` configuration flag is needed.


## 5. Model Preparation

The model owner prepares artifacts during model initialization, before reporting readiness. The shared preparation logic in `components/vlm/text_gen_prepare.py` is called from the VLM load path. As a result, [start-smart-classroom.ps1](../../../Windows/start-smart-classroom.ps1), the desktop application [start-desktop-app.ps1](../../../Windows/start-desktop-app.ps1), and `python main.py` use the same preparation process; none requires a separate launcher step or switch.

On first start, the helper downloads or converts missing artifacts. On later starts, it reuses and validates existing artifacts, rebuilding derived artifacts when source hashes change. Dependent services wait for the model owner’s `/health` endpoint before using the model. Request handlers do not download or convert files.

The following steps describe the model-preparation requirements. Qwen3.5 MTP preparation is implemented; hardware and accuracy qualification remain pending.

1. **Select:** The helper resolves the target, speculative policy, method, and source. It validates explicit settings, then selects a compatible catalog profile. Before downloading, it validates the resolved target, method, source, and revision against artifact metadata and the selected profile. If a recognized method lacks runtime or profile support, the helper uses plain generation in `auto` mode or returns an actionable error in `"on"` mode.

2. **Prepare:** The preparation helper downloads the target’s catalog IR and, for Hub drafters, the pinned drafter files over HTTPS. For `local`, it validates the specified IR directory without downloading a replacement drafter. For a qualified MTP profile, the helper acquires the target’s bundled head and metadata without downloading an unrelated external model. It uses runtime-ready IR directly and converts downloaded sources only through the profile’s qualified recipe. Network failures do not trigger unrelated export attempts.

3. **Validate:** The helper checks required files, integrity, tokenizer and processor compatibility, runtime versions, and target and drafter graph interfaces. It preserves the ordinary target if accelerated export needs separate IR. It records a manifest without overwriting active files or changing legacy model paths.

4. **Activate:** After preparation succeeds, the model owner loads the resolved artifacts and runs warm-up. The service reports readiness only after warm-up succeeds; a completed download alone does not mean the model is ready.

Preparation creates storage on demand, uses resumable staging and per-artifact locks, and publishes only complete, validated artifacts. It bounds retries and checks disk space for weights and conversion intermediates. It keeps prior valid artifacts for rollback. When preparation reuses legacy files, it validates them without claiming unknown provenance.

**Implemented:**

| Artifact | How It Is Prepared and Reused |
| --- | --- |
| Target IR, `models/openvino/<model>/<weight_format>` | Catalog models download the pre-converted `OpenVINO/<model>-<weight_format>-ov` IR (resumable, 3 attempts); other models use the existing local export. Reused when the IR, tokenizer, and detokenizer are present. The manifest records no revision for it because the download is not pinned. The tokenizer IR is refused if a newer openvino-tokenizers release exported it. |
| Drafter source, `models/hf/<org>__<repo>/<commit>` | Configuration and weight files only, at the pinned commit, after a free-space check (2.2x the checkpoint). |
| Drafter IR, `models/openvino/<drafter>/int4` | Exported offline in a separate process; `.prepared.json` records the commit, and the IR is reused while it matches the pin. Eagle3 is exported from a re-keyed temporary copy of the checkpoint with an export-only transformers 4.57.6 installed once (`pip --no-deps --target`) into `models/.export-deps/transformers-4.57.6`, then validated against the target: Eagle3 architecture for a `qwen3_vl` target, matching hidden and vocabulary sizes, and the VLM Eagle3 IR interface (`hidden_states`, `inputs_embeds`, 3-D `position_ids`, `logits`, `d2t`, `eagle3_mode`). |
| Drafter with its own INT4 lm_head, `<drafter>/int4-lmhead` (the one loaded) | The target's INT8 head requantized to INT4 and attached to the drafter, so the runtime no longer reads the full INT8 head on every draft pass. Rebuilt when the SHA-256 of the target or drafter XML changes; if it cannot be built, the stock drafter is used. Not applied to `draft_source: local`. |
| Derived target, `<model>/<weight_format>-dflash` | Only when the stock target lacks `hidden_states_decoder_layers`: weights hard-linked, XML annotated, the ordinary target untouched; rebuilt when the target XML hash changes. |
| Manifest, `models/openvino/.manifests/<model>-<weight_format>.json` | Resolved settings, drafter commit, decision, reason, and timings. |

VLM artifact preparation, recovery, and fallback:

- The model owner calls the preparation process while loading the VLM.
- The preparer stages drafter downloads and derived artifacts in `.partial` directories, then publishes completed artifacts. Each artifact has a `.lock` file that records its owner's process ID and start time, so a lock left by a dead process is recovered even when Windows reuses the ID. When replacing an active drafter or derived artifact, it moves the old artifact aside with a timestamp suffix, never deleted.
- In the `auto` mode, a drafter download, export, or file-system failure falls back to plain generation; a drafter incompatible with the target always fails.

Operator instructions:

1. Install trusted exporter dependencies during software setup.
2. For offline use, prepare missing artifacts while online, or copy prepared artifacts into the expected `models/` locations. Later runs can reuse artifacts that validate successfully without network access.
3. Set proxy environment variables if the network requires a proxy.

Security requirements:

- The preparation flow validates artifact paths and downloaded metadata.
- The preparation module does not execute repository code during artifact preparation.
- Preserve TLS certificate verification.
- The preparation module passes command-line arguments as data, obtains credentials and proxy settings from environment variables, and does not log credentials or other secrets.

## 6. Generation and Failure Policy

During generation, the qualified OpenVINO GenAI runtime uses an external drafter for DFlash/DFlash2 or the target’s bundled prediction head for MTP to propose tokens. The target model verifies the proposals, and the runtime manages acceptance; the application selects one method and enforces readiness and request policy.

At startup, the model-preparation flow validates DFlash/DFlash2 hidden-state outputs and vocabulary alignment, or the MTP head against its target/runtime contract, and checks each method’s runtime and exporter dependencies. The serving application keeps method-specific scheduler and draft-block memory settings separate from plain generation and budgets memory across the target, speculative artifacts, image processing, and other classroom workloads.

The serving application uses speculative decoding only for greedy generation and does not claim image-encoding speedups; image requests run on the same pipeline, including Qwen3.5 MTP. Before streaming, the request handler rejects sampling requests unsupported by an active speculative pipeline rather than silently changing them. With plain generation, the application retains each model's supported behavior. When mode: `"on"` is configured, the model loader requires speculative readiness at startup; this does not require every request to use acceleration.

The serving application supports tool calling with speculative decoding disabled and advertises DFlash/DFlash2/MTP tool support only for profiles that pass the same call/result and streaming tests. For each request, it preserves tool definitions and call IDs, exposes only target-verified tokens, and validates arguments. Before generation, it rejects unsupported model/method/tool combinations rather than silently removing tools or switching methods.


| Condition | `"off"` | `auto` | `"on"` |
| --- | --- | --- | --- |
| No qualified profile for the selected method, including an older model | Use plain model | Use plain model; report reason | Fail with guidance to use plain mode |
| Missing selected files, with a qualified profile available | Acquire target only | Prepare target and method-specific artifacts | Prepare target and method-specific artifacts |
| Target unavailable after permitted preparation | Fail | Fail | Fail |
| Speculative artifact acquisition, conversion, or activation fails | Not applicable | Warm a plain pipeline once, after safe cleanup | Fail |
| Invalid active method, source, or tuning; mismatched target and drafter; or integrity failure | Fail for active target | Fail | Fail |

The model-preparation flow does not download a replacement for a missing or invalid `local` draft. The operator must qualify the accelerator with mode: `"on"`; an auto fallback to plain generation does not count as successful qualification.

If the serving process cannot confirm native cleanup, the operator stops it before recovery. The model loader retains the plain-mode decision for that launch to prevent repeated activation attempts. The request handler does not replay a partially streamed request. To roll back, the operator restores the previous model or artifacts, or disables speculation and restarts; the operator does not delete caches.

The serving application's `/health` endpoint reports the target, tool-call capability, requested and effective methods, drafter identity or target-bundled head, warm-up result, and sanitized fallback reason. Its diagnostics provide detailed revisions, tuning, and runtime versions without exposing private local paths. The application does not log sensitive tool arguments or results by default.


**Implemented** ([text_gen_vlm.py](../../../Windows/components/vlm/text_gen_vlm.py)):

- **Warm-up and fallback.** Readiness follows one two-token greedy generation. Under `auto`, a failed load or warm-up with the drafter discards that in-process pipeline (released and garbage-collected) before the plain pipeline is warmed. The plain decision is kept for the process lifetime.
- **Request policy.** While a speculative pipeline (Eagle3 or DFlash) is active, requests are greedy with the profile's fixed draft window. A request with `temperature > 0` or sampling penalties is rejected with HTTP 400 before generation, and image requests run on the same pipeline. With an `app.language` the profile is not qualified for (Eagle3 under `zh`), the plain pipeline is loaded at startup instead.
- **Client disconnect.** A streamed response closes its token stream as soon as it ends for any reason, including a client disconnect, so native generation stops and the model slot is freed at once instead of when the garbage collector finalizes the abandoned stream.
- **Health.** `GET /health` adds `hub.text_gen.serving` with these fields:
  - `model`, `weight_format`, `device`, `tool_calls`
  - `speculative` (`requested_mode`, `requested_method`, `effective_method`, `draft`, `num_assistant_tokens`, `fallback_reason`)
  - `warmup`
  - `startup` (`prepare_s`, `load_s`, `warmup_s`)
  - `diagnostics` (GenAI version, drafter revision, scheduler)

  The `/health` response replaces absolute local paths in fallback reasons with <path> and identifies a local drafter by its directory name only.

## 7. Processing-Time Statistics

The serving application must record elapsed time at two nested levels: **workflow-stage elapsed time** to measure the classroom experience, and **individual model-call time** to measure inference cost. It must treat these as distinct measurements, retain the existing timing sources described under Collection and Reporting, and implement the remaining coverage.

### Workflow Stages

The application’s timing instrumentation measures each enabled stage from execution start until its output is complete, including preprocessing, model calls, and postprocessing. It records admission and queue wait separately and includes both in request end-to-end latency. It reports stage durations in seconds and per-frame or per-call latency in milliseconds.

| Stage | Processing-Time Boundary and Breakdown | Workload and Additional Statistics |
| --- | --- | --- |
| Media preparation | Input decoding, audio extraction, and chunk preparation | Media duration, chunk count, and preparation seconds; identify work already included in ASR |
| ASR | Consume audio chunks through final transcript persistence; separate input wait, transcription, and optional diarization | Audio seconds, chunk count, ASR, and diarization model-call times; real-time factor = processing seconds / audio seconds for a fixed recorded input |
| VA (video analytics) | First accepted frame through final analytics output for a fixed clip; separate decode, model inference, tracking, and aggregation | Per-stream processed and dropped frames, achieved FPS, p50 and p95 frame latency, and the execution time of each detector, pose, classifier, and re-identification model; report live observation duration separately |
| OCR | Frame or page selection through persisted recognized text; separate extraction, detection, recognition and merge | Frames or pages processed and per-image latency; report board OCR and document OCR separately |
| Summary | Summary execution through final saved summary, including map, fold, and reduce calls when chunking is active | Strategy and chunk count; map-phase duration, including any fold calls; total execution time in seconds, overall time to first token (TTFT), and reduce TTFT; input and output token counts; and decode throughput in tokens per second |
| Mindmap | Summary input preparation through generated, validated, and saved mindmap | Model-call time, validation and persistence time, and any repair attempts; distinguish insufficient-input fallback from model generation |
| Topic segmentation | Transcript preparation through validated and saved topics | Window count, per-window model times, and topic count; measure subsequent embedding and indexing separately |
| Report | Session-data collection through completed report generation and persistence | Collection, model generation, rendering, and export times; identify cached re-selection or reports requiring no model call |
| Content search ingestion | Accepted ingestion task through searchable content | Queue wait time, extraction and video captioning times, embedding and indexing times; document, page, frame, and chunk counts |
| RAG Q&A | Accepted question through final answer | Retrieval, reranking, prompt preparation and generation times; TTFT and input and output tokens |
| Grading | Accepted grading task through completed result | Layout detection, OCR and VLM calls, and result assembly; page and question counts and retry times |

The timing instrumentation records workflow wall time from workflow start until all requested outputs are complete; it **does not sum stage durations to infer end-to-end latency**, because ASR, VA, and board OCR may overlap and stages may share the text-generation queue. It also records which stages were enabled and whether the run was live, recorded-media replay, or an isolated stage benchmark. For live workflows, it separately records the time from input completion to final output, so recording duration is not counted as inference cost.

### Individual Models

The timing instrumentation records each invocation at the model owner, along with its parent stage and an opaque run or request identifier. It groups measurements by actual model or artifact revision, precision, device, hardware platform, runtime version, and effective decoding method. It preserves each call’s attribution to its workflow—Summary, Mindmap, segmentation, reports, captioning, Q&A, or grading—even when those workflows share a Qwen model.

| Model-Call Statistic | Definition |
| --- | --- |
| Call latency | Monotonic elapsed time from model invocation to final output, excluding admission wait; include preprocessing and postprocessing performed inside that boundary and label it accordingly. Report runtime inference-only time separately when available. |
| Generative breakdown | Input and image preparation, prefill and decode times when exposed by the runtime; model TTFT, prompt and output token counts and decode tokens per second. Do not estimate unavailable subphase timings by subtraction. |
| Non-generative breakdown | Inference time per audio chunk, image, frame, or batch, with input dimensions or duration when applicable, and batch size; keep detection, recognition, embedding, and reranker models separate. |
| Speculative decoding | Requested method, effective method, and fallback status; target verification time, drafter or bundled-head time, and accepted and proposed tokens when exposed by the runtime. These are subphases of the call, not extra workflow stages. |
| Startup | Record download, conversion, model loading and compilation, and warm-up times separately from warm-request latency. |

The timing instrumentation follows the generation metric definitions and source/boundary rules in the [benchmark metrics section](../context-bench/context-bench-design.md#7-iteration-metrics-and-comparison-rules). It reports model TTFT separately from client-visible TTFT, which also includes request preparation, queueing, transport, and earlier map calls. Each process records its own durations; the instrumentation does not subtract monotonic timestamps across processes or hosts.

The performance-reporting workflow reports call-latency mean, p50, and p95; TTFT; and decode throughput for every model/precision in Section 2 across the platform/method matrix in Section 8. It includes the installed model as the compatibility baseline and reports applicable per-call statistics for ASR, VA, OCR, embedding, and reranker models under their configured identities.

### Collection and Reporting

The timing instrumentation reuses existing measurements without changing their meanings: [ASR](../../../Windows/components/asr_component.py) writes `performance.transcription_time`; [Summary](../../../Windows/components/summarizer_component.py) writes `performance.summarizer_time`, `performance.summary_map_time`, `performance.ttft`, `performance.reduce_ttft`, and `performance.tps`; [Report](../../../Windows/components/report_generator/report_generator.py) writes collection, generation, and total times. These are component timers, not isolated model benchmarks. ASR includes upstream input waiting and optional diarization; the report generation timer also includes rendering and persistence. [Hardware monitoring](../../../Windows/monitoring/monitor.py) provides utilization, memory, and power samples - not model-call latency. The instrumentation keeps these existing fields compatible and adds the missing stage- and call-level measurements at their owning boundaries.

The timing instrumentation retains a separate record for each stage attempt and model invocation, so repeated calls, retries, and failures are not overwritten by a last-value metric. It uses monotonic clocks and finalizes timers on success, failure, or cancellation; for streaming, it measures through generator completion rather than creation. The timing instrumentation must record client-cancellation time separately from any continuing native inference and retain only status, counts, and timing metadata, with bounded retention. Records must exclude prompts, transcripts, images, tool arguments and results, student identifiers, credentials, and private paths. A failed stage's `error_detail` keeps the exception message with absolute local paths redacted, cut to 300 characters.

The statistics reporter publishes, for each stage and model group, the successful sample count, total processing time, mean, minimum, maximum, p50, and p95. It reports failed and cancelled attempt counts and elapsed times separately. It marks skipped stages as not applicable and missing, unsupported, or negative-sentinel measurements as unavailable—not zero. It preserves raw samples, units, measurement boundaries, and the percentile calculation method, and labels a single run as an observation rather than a latency distribution.

The benchmark and reporting process compares repeated warm runs with the same fixed inputs, prompt templates, token limits, chunking, concurrency, media settings, and enabled stages. For each result, it publishes hardware, driver, runtime, and artifact versions; input durations or dimensions; token counts; warm-up and repetition counts; quality outcomes; and an evidence link. It reports isolated model benchmarks separately from concurrent classroom runs and cold-start measurements. It calculates speedup only for matched plain and accelerated cases using the same latency statistic, and reports fallback runs as plain generation—not accelerated success.

### Implemented Coverage

See [processing-time-statistics.md](../processing-time-statistics/processing-time-statistics.md) for fields and usage.

| Level | Recorded Now | Not Recorded Yet |
| --- | --- | --- |
| Workflow stages ([stage_tracker.py](../../../Windows/utils/stage_tracker.py)) | `transcribe`, `va`, `board_ocr`, `summarize`, `mindmap`, `segmentation`, and `report`: one record per attempt with status, UTC start and end times, and duration | Media preparation, content-search ingestion, and the retrieval and reranking breakdown for RAG Q&A |
| Stages seen through model calls | `document_ocr`, `board_summary` (`POST /board-ocr/summary`) and `/v1/chat/completions` callers named by the `X-SC-Stage` header (`qa`, `video_captioning`, or `grading`; other callers are identified as `chat_api`) | — |
| Model calls ([processing_stats.py](../../../Windows/utils/processing_stats.py)) | `text_gen`, `asr`, and `ocr`: queue wait, latency, status, client cancellation, and for text generation, TTFT, TPOT, decode, prefill and output tokens per second, token counts, and DFlash accepted and drafted tokens | VA, embedding, reranking, and grading layout and detection models; target-versus-drafter time inside a DFlash call |
| Startup | Preparation, load, and warm-up seconds in the `/health` endpoint | A history for each run |

The processing-statistics collector retains the latest 4,000 records of each kind in memory for `GET /processing-stats` and appends them to `<session>/logs/stage_events.jsonl` and `<session>/logs/model_calls.jsonl`. It records timing metadata only. The [processing_stats_report.py](../../../Windows/Scripts/processing_stats_report.py) script builds a per-session timeline from either source, counts each stage attempt's associated model calls, including failed and cancelled calls, lists every text-generation call of a session with its TTFT, TPOT and tokens per second, tabulates the same per stage and per model across sessions, and computes DFlash acceptance only from calls that report both counters.

## 8. Release Decisions

| Area | Required Checks |
| --- | --- |
| Backward compatibility | Load unchanged older configurations; retain existing key names, token and queue limits, chunking, segmentation, and service endpoints. Test supported providers and model IDs, precisions, and local paths; do not download a drafter when off; use plain fallback in `auto`; check for regressions after runtime dependency upgrades. |
| Model/precision support | For every profile in Section 2, test empty-cache preparation, warm-up and later reuse of the validated artifacts. Plain mode downloads only the target; an accelerated case requires all artifacts for its selected method. |
| Port-8000 tool calling | End-to-end function definition -> tool call -> tool result -> answer through `/v1/chat/completions`, with streaming and non-streaming clients. Test `none`/`auto`/`required`/named choices, multiple-call IDs, `parallel_tool_calls: false`, full history, malformed arguments, missing results, truncation, unsupported models and no server-side tool execution. |
| Speculative configuration | Test missing and mode-only blocks, all supported methods, explicit and default Hub revisions, local path handling, and tuning bounds. Reject cross-model drafters and MTP with an external draft source or model; bundled MTP must never trigger a z-lab download. |
| Classroom quality | Test streaming and non-streaming text and images, supported thinking and structured output, cancellation, and queue limits; summaries, mind maps, segmentation, reports, RAG Q&A, video summaries, and grading. |
| Reliability | Test invalid pairs and corrupt artifacts, network and disk failures, interrupted and concurrent downloads, safe startup fallback, and identical preparation from both launchers on Windows PowerShell versions 5.1 and 7. |
| Performance | Compare plain and accelerated modes repeatedly for answer quality, time to first token, tokens per second, end-to-end p50 and p95 latency, and peak memory. Separate download, conversion, and compile time from inference. |
| Timing statistics | Publish stage and individual-model results defined in Section 7 for each qualification case. Verify attribution for shared models, overlapping stages, streaming completion, retries, failures, cancellation, and skipped stages; retain raw samples and do not present unavailable timings as zero or pending cases as measured. |
| Context bench | Complete the [benchmark implementation and tests](../context-bench/context-bench-design.md#10-implementation-sequence-and-verification); verify exact-context and matched-profile measurements, separate accuracy verdicts, interruption-safe evidence, nonzero failure exits, and unchanged source and serving artifacts. Publish model-call evidence without presenting it as full classroom-workflow timing. |

GPU qualification:
- Platforms: Arrow Lake and Panther Lake.
- Qwen3.5-9B: INT4 and INT8 in plain mode on both platforms (four cases).
- Qwen3.6: INT4 in plain mode and with DFlash on both platforms.
- Qwen3.8: INT4 and INT8 in plain mode and with DFlash2 on both platforms.
- The Qwen3.6 and Qwen3.8 cases total 12; the GPU matrix totals 16.
- These are planned qualification cases, not reported passes. DFlash2 remains blocked until its adapter and runtime are qualified.

Additional coverage:
- Add older-model regression cases.
- Qualify MTP separately for each advertised target, precision, and platform. Do not infer support across methods.
- Include tool-call round trips for tool-capable profiles.
- Qualify CPU separately from GPU.

Result details:
Record the exact hardware, artifacts, runtime and driver versions, and workload settings for every result.

**Verified so far (not qualification):**
- **Unit tests** (`components/tests`, `tests/unit`, `model_manager/test`) cover:
  - configuration resolution, preparation, and locking;
  - the tool-call contract, including truncation, forced choices, malformed history, and stream failures
  - stream slot release and the timing records.
- **Tool calls, end to end:** one Arc 140T (Arrow Lake) passed 10 tool-call scenarios through the `openai` client on four profiles:
  - Qwen3-VL-8B, Qwen3.5-9B and Qwen3.8-27B INT4, plain
  - Qwen3.6-35B-A3B INT4 with DFlash.
- **DFlash performance:** `context_bench` measured DFlash for Qwen3.6 INT4 on the same machine.
- **Eagle3, end to end on Arc 140T** (Qwen3-VL-8B INT4, `mode: auto`):
  - first start downloaded, exported and validated the drafter; later starts reused it;
  - image streaming with usage, `length` finish, `temperature: 0.7` -> HTTP 400, client cancel, tool round trip, and streamed and `required` tool calls all passed;
  - an unreachable Hub fell back to plain under `auto` (reason in `/health`) and failed startup under `"on"`; a Qwen3-VL-4B drafter given as `draft_source: local` failed startup (hidden size 2560 vs 4096);
  - Chinese classroom summaries gained nothing (both the AQ and the taobao-mnn drafter were slower than plain), hence the `en`-only qualification.
- **Qwen3.5-9B MTP, end to end on Arc 140T** (INT4, GenAI 2026.4.1, `mode: "on"`, one replayed English lesson per setting):
  - `/health` reported `effective_method: mtp`; streamed text and an image question both worked;
  - at equal queue priority, MTP was not a net gain over plain for the lesson's text generation.
- **Qwen3.5-9B DFlash on Arc 140T** (INT4 and INT8, GenAI 2026.4.1):
  - first start downloaded, exported and validated the drafter; the stock target IR already carried `hidden_states_decoder_layers`, so no derived target was created; INT8 reused the drafter export and built only its own lm_head copy;
  - offline, DFlash decoded faster than plain and MTP on both precisions, English and Chinese; on INT8 k=7 was faster than k=5, hence the INT8 default k=7;
  - through `VLMTextGen` (`mode: "on"`, INT4 and INT8): `/health` reported `effective_method: dflash`; text, JSON-schema and image requests worked, and `temperature: 0.7` was rejected.

The Arrow Lake and Panther Lake matrix above remains planned.

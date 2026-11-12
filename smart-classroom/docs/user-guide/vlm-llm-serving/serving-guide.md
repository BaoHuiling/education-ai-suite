# VLM/LLM Serving Guide

This guide shows how to choose the text-generation model for Smart Classroom
and how to turn on the optional DFlash accelerator.

All settings are under `models.text_gen` in
[config.yaml](../../../Windows/config.yaml). Restart the application after you
change them.

## Recommended Configuration

Pick the row that matches your platform and system memory:

| Platform | Memory | Model | Accelerator |
| --- | --- | --- | --- |
| Arrow Lake (ARL) | 32 GB | Qwen3.5-9B INT4 | DFlash, `num_assistant_tokens: 7` |
| Arrow Lake (ARL) | 64 GB | Qwen3.6-35B-A3B INT4 | None |
| Panther Lake (PTL) | 32 GB | Qwen3.5-9B INT4 | DFlash, `num_assistant_tokens: 7` |
| Panther Lake (PTL) | 64 GB | Qwen3.6-35B-A3B INT4 | DFlash, `num_assistant_tokens: 5` |

Copy the matching block into `config.yaml`. Keep the other keys under
`models.text_gen` as they are.

**ARL or PTL with 32 GB: Qwen3.5-9B INT4 with DFlash**

```yaml
models:
  text_gen:
    provider: vlm
    vlm_name: Qwen/Qwen3.5-9B
    device: GPU
    weight_format: int4
    speculative:
      mode: "on"
      method: dflash
      draft_source: null
      draft_model: null
      draft_revision: null
      num_assistant_tokens: 7
```

**ARL with 64 GB: Qwen3.6-35B-A3B INT4**

```yaml
models:
  text_gen:
    provider: vlm
    vlm_name: Qwen/Qwen3.6-35B-A3B
    device: GPU
    weight_format: int4
    speculative:
      mode: "off"
```

**PTL with 64 GB: Qwen3.6-35B-A3B INT4 with DFlash**

```yaml
models:
  text_gen:
    provider: vlm
    vlm_name: Qwen/Qwen3.6-35B-A3B
    device: GPU
    weight_format: int4
    speculative:
      mode: "on"
      method: dflash
      draft_source: null
      draft_model: null
      draft_revision: null
      num_assistant_tokens: 5
```

## Available Models

| `vlm_name` | `weight_format` | Optional accelerator |
| --- | --- | --- |
| `Qwen/Qwen3-VL-8B-Instruct` | `int4`, `int8` | None |
| `Qwen/Qwen3.5-9B` (default) | `int4`, `int8` | DFlash |
| `Qwen/Qwen3.6-35B-A3B` | `int4` | DFlash |
| `Qwen/Qwen3.8-27B` | `int4`, `int8` | None |

For a model without an accelerator, set `speculative.mode: "off"`.

You do not need to download anything by hand. On first start, the application
downloads the pre-converted OpenVINO model into
`models/openvino/<model>/<weight_format>`. Later starts reuse it.

## Speculative Decoding (DFlash)

Speculative decoding makes generation faster. A small draft model proposes
several tokens, and the main model checks them in a single pass. DFlash is
available for Qwen3.5-9B (INT4 and INT8) and Qwen3.6-35B-A3B (INT4).

### Settings

```yaml
    speculative:
      mode: "on"                # "off" | auto | "on"
      method: dflash
      draft_source: null
      draft_model: null
      draft_revision: null
      num_assistant_tokens: 7   # null = default for the model
```

| Setting | Value | Meaning |
| --- | --- | --- |
| `mode` | `"off"` | Plain generation, no accelerator. Same as omitting the `speculative` block. |
| | `auto` | Use DFlash when it can be prepared and loaded. Otherwise, fall back to plain generation and report the reason in `/health`. |
| | `"on"` | DFlash is required. If it cannot run, startup fails with an error that explains why. |
| `method` | `dflash` | The accelerator to use. |
| `draft_source`, `draft_model`, `draft_revision` | `null` | Use the tested DFlash draft model. It is downloaded and prepared automatically. |
| `num_assistant_tokens` | `null` or 1–15 | Number of tokens drafted per step. `null` uses 5 (7 for Qwen3.5-9B INT8). |

Put quotes around `"off"` and `"on"`. Without quotes, YAML reads them as
true/false.

### Choosing `num_assistant_tokens`

Start with the value in [Recommended Configuration](#recommended-configuration).
A larger value drafts more tokens per step. It helps when most drafts are
accepted, for example in code, math or structured output. It wastes work when
many drafts are rejected. To tune it, change the value by 1 or 2, restart, and
compare the speed on your own lessons.

### What changes while DFlash is on

* **Greedy output only.** A request with `temperature` above 0, or with
  sampling penalties, is rejected with HTTP 400. Smart Classroom's own features
  already use `temperature: 0`. This only affects your own clients on port 8000.
* **Images still work.** Requests with images use the same pipeline.
* **Slightly different wording.** The output can differ in places from plain
  generation, with no consistent effect on accuracy.
* **Longer first start.** The first start with DFlash downloads and prepares
  the draft model, which takes a few minutes. Later starts reuse it.

### Check that DFlash is active

Open `http://127.0.0.1:8000/health` after startup. Look at
`hub.text_gen.serving.speculative`:

```json
"speculative": {
  "requested_mode": "on",
  "requested_method": "dflash",
  "effective_method": "dflash",
  "draft": "z-lab/Qwen3.5-9B-DFlash@5fc3b3d47476",
  "num_assistant_tokens": 7,
  "fallback_reason": null
}
```

`"effective_method": "dflash"` means DFlash is running. With `mode: auto`,
`"effective_method": "plain"` means the application fell back to plain
generation; `fallback_reason` says why.

### Turn DFlash off

Set `mode: "off"` and restart. The prepared files stay on disk for later use.

## Model Files and Offline Use

The application prepares the configured model at startup, whether you start it
with [start-smart-classroom.ps1](../../../Windows/start-smart-classroom.ps1),
the desktop app, or `python main.py`.

| Files | Location |
| --- | --- |
| Main model | `models/openvino/<model>/<weight_format>` |
| DFlash draft model | `models/openvino/<draft model>/` and `models/hf/` |

* An interrupted download resumes on the next start.
* To run offline, start once with network access, or copy the prepared
  `models/` folder (including `models/hf/`) to the offline machine.

## Tool Calling on Port 8000

`POST http://127.0.0.1:8000/v1/chat/completions` accepts OpenAI-style function
tools on all models listed above. The server returns the tool calls; **it never
runs them**. Your client runs the tools and sends the results back.

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

* `tool_choice` accepts `"none"`, `"auto"` (default), `"required"`, or a named
  function.
* Set `parallel_tool_calls: false` to get at most one call per turn.
* Add `"enable_thinking": false` (in `extra_body`) to skip reasoning on
  Qwen3.5, Qwen3.6 and Qwen3.8 for faster calls. Reasoning, when on, is
  returned in `message.reasoning_content`.
* `$ref` and `pattern` in tool schemas, and images given by URL or file path,
  are rejected with HTTP 400. Send images as `data:image/` URLs.
* A call that does not match the tool's schema returns HTTP 502
  (`tool_call_validation_failed`).

Design details are in
[vlm-llm-serving-design.md](../../dev-guide/vlm-llm-serving/vlm-llm-serving-design.md).

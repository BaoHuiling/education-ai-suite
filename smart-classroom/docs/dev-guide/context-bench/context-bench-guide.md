# Context Bench

`components/context_bench/` is an offline, text-only OpenVINO LLM benchmark. It answers, per
model, precision, device, context, task and runtime profile:

1. Can this machine process the context within the configured memory budgets?
2. Which profile (KV precision, stateful vs paged pipeline, scheduler, DFlash or Eagle3
   speculative decoding and its window) has the best measured latency?
3. Optionally (`--accuracy`): how does a profile affect synthetic retrieval accuracy and
   generation fidelity relative to a baseline profile?

It never loads the classroom application, starts services, edits application configuration
or chooses production defaults. It measures individual model calls, not classroom workflow
time. Results are observations on the machine that produced them, not qualification.

For implementation details, see [Context Bench Design](context-bench-design.md).

## Quick start

Run from the app root, `smart-classroom/Windows/`. The benchmark uses the backend
venv one level above it (`smart-classroom/smartclassroom`) that
`setup-smart-classroom.ps1` creates; the
application's `requirements.txt` pins everything it needs. Then:

```powershell
# Plan only: settings, redacted locations, the case grid (c01, c02, ...) and probe counts.
# No inference, downloads or files.
.\components\context_bench\run_benchmark.ps1 --list-profiles

# Smoke test, then a repeated comparison
.\components\context_bench\run_benchmark.ps1 --profiles paged_min dflash_k7 --contexts 8000 --throughput-task math --warmup 0 --iterations 1
.\components\context_bench\run_benchmark.ps1 --profiles paged_min dflash_k7 --contexts 8000 --throughput-task math --warmup 1 --iterations 3 --iteration-gap 0

# Accuracy
.\components\context_bench\run_benchmark.ps1 --profiles paged_min dflash_k7 --contexts 8000 --accuracy --accuracy-tasks niah_single fact_sheet --accuracy-depths 0.5 --accuracy-samples 1

# All four INT4 models, performance and accuracy, one Markdown report (see Cross-model evaluation)
.\components\context_bench\run_evaluation.ps1 --quick
```

The launcher forwards every argument to `python -m components.context_bench.benchmark` using
the venv's Python, resolves a relative `--config` against your current directory, runs from
the Smart Classroom root and returns Python's exit code. With the right interpreter active you
can call the module directly; `--list-profiles` and the tests need neither OpenVINO nor
transformers:

```powershell
python -m unittest discover -s components/tests -p "test_context_bench_*.py"
python -m components.context_bench.benchmark --config components/context_bench/config_qwen3.6_35b.yaml --list-profiles
```

| Exit code | Meaning |
| --- | --- |
| 0 | Valid plan, or a completed run in which every case is `ok` |
| 1 | Failed, partial or unqualified cases (for example a weight-precision label mismatch), or an orchestration/output-write failure |
| 2 | Invalid arguments or configuration, or missing required packages |
| 130 | Ctrl-C, after the child was cleaned up and evidence checkpointed |

## Configurations and artifacts

The benchmark only reads artifacts prepared by the serving workflow
(`start-smart-classroom.ps1`) or an explicit export; it never downloads, exports or repairs
files. Missing files are reported per case as `missing_ir` with a hint.

| Config | Model / precision | Profiles | Default run |
| --- | --- | --- | --- |
| `config_qwen3.6_35b.yaml` (default) | Qwen3.6-35B-A3B INT4, `models/openvino/Qwen3.6-35B-A3B/int4` | `paged_min`, `dflash_k3/5/7/15` (draft `models/openvino/Qwen3.6-35B-A3B-DFlash/int4-lmhead`) | 35 cases: contexts 0 and 8000; `code`, `math`, `chat`, `summary_2s`; 512-token ceiling; 30 s gap. `--accuracy`: 5 cases |
| `config_qwen3.8_27b.yaml` | Qwen3.8-27B INT4 (`--weight-format int8` for INT8), `models/openvino/Qwen3.8-27B/<weight_format>` | `paged_min` (plain only) | 7 cases: same contexts, tasks, ceiling and gap. `--accuracy`: 1 case |
| `config_qwen3.5_9b.yaml` | Qwen3.5-9B INT4 (`--weight-format int8` for INT8), `models/openvino/Qwen3.5-9B/<weight_format>` | `paged_min`, `dflash_k3/5/7/15` (draft `models/openvino/Qwen3.5-9B-DFlash/int4-lmhead`) | 35 cases: same contexts, tasks, ceiling and gap. `--accuracy`: 5 cases |
| `config_qwen3_8b.yaml` | Qwen3-VL-8B-Instruct INT4 (`--weight-format int8` for INT8), `models/openvino/Qwen3-VL-8B-Instruct/<weight_format>` | `paged_min` (plain only) | 7 cases: same contexts, tasks, ceiling and gap. `--accuracy`: 1 case |

One config per model: the weight formats differ only in the artifact, so `--weight-format`
selects it (the path is derived) rather than a second, otherwise identical file. All four share
the core settings of `config_qwen3.6_35b.yaml` -- INT4 weights on GPU, contexts 0 and 8,000,
`throughput_task: [code, math, chat, summary_2s]`, a 512-token ceiling, 1 warm-up + 3 measured
iterations, a 30 s iteration gap, the timeout and memory budgets, and an identical accuracy
section that matches the reference configuration -- so a default run of each is directly
comparable; a test keeps them aligned. Only the model, its output directory and its profiles
differ: DFlash profiles (`dflash_k*`) exist only in the Qwen3.6 and Qwen3.5 configs, and
`model_dirs` and `speculative_reference` only in the Qwen3.6 config. Every speculative profile shares its config's `paged_min` OpenVINO
properties and scheduler, so the window k is the only variable.
`config_qwen3_8b.yaml` benchmarks the application's default `text_gen` model through its VLM
export (`openvino_language_model.xml`, so `VLMPipeline`) with text prompts only.

### Test steps (c01, c02, ...)

A *case* (test step) is one combination of model, context, task and profile, run in its own
child process: load, warm-up(s), then the measured generations (throughput) or the probes
(`--accuracy`), with memory sampled throughout. Cases are numbered in execution order, which is
row by row: every profile runs a task back to back before the next task starts, so a
baseline and its speculative profiles see the same thermal conditions. IDs are zero-padded to
the plan size, so they sort. The ID appears in the console banner (`[c07 ...]`), in
`results.json` (`plan[*].case_id`, `cases[*].case_id`) and in the `CASES` section, failure lines
and not-run lines of `report.txt`. CLI filters renumber the plan; `--list-profiles` prints the
exact grid, with its IDs, before anything loads.

Each config documents its own steps in its header -- the grids, what every context, task and
profile means, and the accuracy plan -- and a test keeps those grids equal to what the
scripts plan. The default plans:

`config_qwen3.6_35b.yaml` -- 1 warm-up + 3 measured generations per case, up to 512 tokens,
30 s idle before each measured one. `none` is the task prompt alone (the DFlash blog's
setting); `8,000 tokens` is a synthetic classroom transcript followed by the task.

```text
  Context           Task       | paged_min  dflash_k3  dflash_k5  dflash_k7  dflash_k15
  none (task only)  code       | c01        c02        c03        c04        c05
  none (task only)  math       | c06        c07        c08        c09        c10
  none (task only)  chat       | c11        c12        c13        c14        c15
  8,000 tokens      code       | c16        c17        c18        c19        c20
  8,000 tokens      math       | c21        c22        c23        c24        c25
  8,000 tokens      chat       | c26        c27        c28        c29        c30
  8,000 tokens      summary_2s | c31        c32        c33        c34        c35
```

The other three configs run the same rows with the same repetition, ceiling and gap; only the
profile columns differ.

`config_qwen3.8_27b.yaml`:

```text
  Context           Task       | paged_min
  none (task only)  code       | c01
  none (task only)  math       | c02
  none (task only)  chat       | c03
  8,000 tokens      code       | c04
  8,000 tokens      math       | c05
  8,000 tokens      chat       | c06
  8,000 tokens      summary_2s | c07
```

`config_qwen3.5_9b.yaml` runs the same grid as `config_qwen3.6_35b.yaml` (`paged_min` and
`dflash_k3/5/7/15`, c01-c35).

`config_qwen3_8b.yaml`:

```text
  Context           Task       | paged_min
  none (task only)  code       | c01
  none (task only)  math       | c02
  none (task only)  chat       | c03
  8,000 tokens      code       | c04
  8,000 tokens      math       | c05
  8,000 tokens      chat       | c06
  8,000 tokens      summary_2s | c07
```

With `--accuracy` the throughput cases are replaced by one accuracy case per profile at every
context with a transcript (context 0 has nowhere to plant a fact): `8,000 tokens | accuracy`,
`c01` ... one per profile (5, 1, 5 and 1 cases). Each runs 1 warm-up and 32 probes: 28 RULER
retrieval probes scored against planted ground truth and 4 generation probes scored against
the baseline profile's answer to the identical prompt.

| Task | What each case decodes |
| --- | --- |
| `code` | A Python module with tests (HumanEval-like); after the transcript, or alone at context 0 |
| `math` | Six worked physics/arithmetic/algebra problems (GSM8K-like); same placement |
| `chat` | Eight open-ended assistant requests (MT-Bench-like); same placement |
| `summary_2s` | A two-sentence summary of the transcript (~45 tokens); transcript contexts only |
| `classroom_summary` | The application's summarizer request, a long Markdown summary; transcript contexts only, and needs about 1024 output tokens to finish |
| accuracy | The probes of the `accuracy` section; no timed iterations, latency and memory come from the probes |

Two selected profiles that resolve to the same configuration (typically `--assistant-tokens K`
over a k-sweep) are refused, since every case would run twice; narrow with `--profiles`.

All use one warm-up and three measured iterations. A model name without a `model_dirs`
entry resolves to `<models_base_path>/<provider>/<name after the last />/<weight_format>`,
the same layout the serving preparation workflow uses.
`--weight-format` changes the label and that derived path, never an explicit `model_dirs`
directory; the IR's actual weight compression (NNCF rt_info) is always reported and a label
mismatch is flagged.

Speculative decoding is benchmarked only where serving has a profile for it
(`components/vlm/text_gen_profiles.py`): DFlash on Qwen3.6-35B-A3B and Qwen3.5-9B with the
z-lab drafters. The Qwen3.8 and Qwen3-VL-8B configs are plain only; serving's Eagle3 profile
for Qwen3-VL-8B-Instruct can still be measured by adding `eagle3` profiles (see
[Eagle3 artifacts](#eagle3-artifacts)).

### DFlash artifacts

Start the application once with `models.text_gen.vlm_name: Qwen/Qwen3.6-35B-A3B`,
`weight_format: int4` and `speculative: {mode: auto, method: dflash}`; the serving
preparation then exports the drafter to `models/openvino/Qwen3.6-35B-A3B-DFlash/int4` and
derives `.../int4-lmhead`, the `dflash.model` of the config: the same drafter with its own int4
lm_head, requantized from the target's int8 head, so the runtime no longer reads the full int8
head on every draft pass (2-6% cheaper verification passes in the A/B, acceptance unchanged).
To measure the stock drafter, point `dflash.model` at `.../int4`. DFlash also requires the target IR to carry the model-level
rt_info `hidden_states_decoder_layers` (the current `OpenVINO/Qwen3.6-35B-A3B-int4-ov` export
does). For an older export without it, serving derives
`models/openvino/Qwen3.6-35B-A3B/int4-dflash` (same weights, annotated XML); the benchmark
never modifies the target, so preflight fails with `missing_ir` until `model_dirs` points
there.

Qwen3.5-9B is prepared the same way (`vlm_name: Qwen/Qwen3.5-9B`, `weight_format: int4`,
`speculative: {mode: auto, method: dflash}`): the drafter lands in
`models/openvino/Qwen3.5-9B-DFlash/int4` and its own-head copy in `.../int4-lmhead`, the
config's `dflash.model`. The head is built from the target it serves: for `--weight-format
int8`, serving builds `.../int4-lmhead-int8`, so point `dflash.model` there to measure what
serving runs.

### Eagle3 artifacts

Start the application once with `models.text_gen.vlm_name: Qwen/Qwen3-VL-8B-Instruct`,
`weight_format: int4`, `app.language: en` and `speculative: {mode: auto}` (method `auto` or
`eagle3`); the serving preparation exports the pinned `AQ-MedAI/Qwen3-VL-8B-Instruct-eagle3`
drafter to `models/openvino/Qwen3-VL-8B-Instruct-eagle3/int4`, the `eagle3.model` to use
in a profile (`config_qwen3_8b.yaml` ships none). Eagle3 runs on the ordinary target (the runtime taps its hidden states), so nothing is
derived from it. Preflight applies serving's pair check -- an Eagle3 architecture trained for
the target's `model_type`, matching hidden size and vocabulary, the VLM drafter interface
(`hidden_states`, `inputs_embeds`, 3D `position_ids` in; `logits`, `d2t` out) and the
`eagle3_mode` marker -- and reports a mismatch as an `artifact` case error. Eagle3 on a VLM
target needs openvino-genai >= 2026.4. Serving's profile keeps the plugin's default KV
precision; `config_qwen3_8b.yaml` keeps `KV_CACHE_PRECISION: f16` like the other configs,
and `--pipeline-config KV_CACHE_PRECISION=` removes it from every profile at once.

## Cross-model evaluation

`run_evaluation.ps1` (`python -m components.context_bench.evaluate`) measures performance and
accuracy of the four INT4 text-generation models under one shared workload and writes a single
Markdown report. It drives `benchmark` once per model and suite, reusing the configs above, so
every number in the report comes from a normal `results.json`. Its `throughput` suite is the
configs' shared default grid over every profile of each config, so each config's
documented test steps all run. The timing suites (`throughput`,
`classroom`) idle 30 s before each measured generation, as the configs do; `accuracy` and
`--quick` run back to back. Back to back, the iGPU's clocks sag over the iterations and
speculative decoding, which works the GPU harder per step, loses more than its plain baseline: on the Arc B390 its
k=3 TPOT rose 11.6 -> 13.7 ms over three iterations while the baseline stayed at 21.8 ms, and
the code speedup read 1.72x instead of 1.82x. `--iteration-gap 0` measures that sustained load.

| Model key | Config | Profiles |
| --- | --- | --- |
| `qwen3-vl-8b` | `config_qwen3_8b.yaml` | `paged_min` |
| `qwen3.5-9b` | `config_qwen3.5_9b.yaml` | `paged_min` (DFlash off), `dflash_k3`, `dflash_k5`, `dflash_k7`, `dflash_k15` (DFlash on) |
| `qwen3.6-35b-a3b` | `config_qwen3.6_35b.yaml` | `paged_min` (DFlash off), `dflash_k3`, `dflash_k5`, `dflash_k7`, `dflash_k15` (DFlash on) |
| `qwen3.8-27b` | `config_qwen3.8_27b.yaml` | `paged_min` |

| Suite | Workload (identical for every model) |
| --- | --- |
| `throughput` | `code`, `math`, `chat` at context 0 and 8,000 tokens, `summary_2s` at 8,000; up to 512 tokens; 1 warm-up + 3 measured |
| `classroom` | `classroom_summary` (the application's summarizer request) at 8,000 tokens; up to 1024 tokens; 1 warm-up + 3 measured |
| `accuracy` | `--accuracy` at 8,000 tokens: 28 RULER retrieval probes and 4 generation probes per profile |

```powershell
# Every plan, validated by the benchmark's own --list-profiles; loads nothing
.\components\context_bench\run_evaluation.ps1 --list-profiles

# Smoke test of the whole pipeline (no warm-up, one iteration, 1 sample at depth 0.5)
.\components\context_bench\run_evaluation.ps1 --quick

# The full evaluation (all models, all suites)
.\components\context_bench\run_evaluation.ps1

# Subsets and variations
.\components\context_bench\run_evaluation.ps1 --spec-k 5 7               # plain + only the k=5/7 profiles
.\components\context_bench\run_evaluation.ps1 --models qwen3-vl-8b qwen3.5-9b --suites throughput accuracy
.\components\context_bench\run_evaluation.ps1 --iteration-gap 0       # sustained instead of burst

# Re-render the Markdown report of an existing evaluation
.\components\context_bench\run_evaluation.ps1 --report monitoring/executionlogs/context_bench/evaluation/<timestamp>
```

| Option | Meaning |
| --- | --- |
| `--models K ...` | Model keys from the table above (default: all four) |
| `--suites S ...` | `throughput`, `classroom`, `accuracy` (default: all three) |
| `--spec-k K ...` | Run only the speculative (DFlash/Eagle3) profiles with these windows, plus every plain profile; each K must be shipped by at least one selected model (DFlash 3, 5, 7, 15). Default: every profile |
| `--weight-format` | Weights of every model (default `int4`); selects the derived artifact directory |
| `--device` | Override every config's device (`CPU`, `GPU`, `GPU.1`) |
| `--iteration-gap S` | Untimed idle before each measured generation, every suite (default 30 s for `throughput`/`classroom`, 0 for `accuracy` and `--quick`; 0 = sustained load) |
| `--quick` | Smoke test: zero warm-up, one measured iteration, `throughput` reduced to `summary_2s` at 128 tokens (`classroom` keeps its 1024-token ceiling), reduced accuracy plan |
| `--output-dir` | Evaluation base directory (default `monitoring/executionlogs/context_bench/evaluation`) |
| `--list-profiles` | Print each invocation's plan and exit |
| `--report DIR` | Only re-render `evaluation_report.md` from `DIR/evaluation.json` |

Speculative decoding off and on run in the same invocation, so each DFlash/Eagle3 case has its
matched `paged_min` baseline (same artifact, prompt hash, task, device and scheduler) and the
speedup isolates the drafter. The artifacts must already be prepared (see
[DFlash artifacts](#dflash-artifacts) and [Eagle3 artifacts](#eagle3-artifacts));
a model that is not prepared finishes its cases as `missing_ir` with a preparation hint, the
other models still run, and the report lists it under failures. The evaluation exits 0 only
when every invocation exited 0; Ctrl-C stops after the benchmark has cleaned up and checkpointed,
and the report then covers the finished runs (exit 130).

Each evaluation writes `<output-dir>/<UTC-timestamp>/`:

- `evaluation.json` -- the manifest: options, and per invocation its arguments, exit code,
  duration and benchmark run directory.
- `<model>/<suite>/console.log` and `<model>/<suite>/<run-id>/{results.json,report.txt}` --
  the benchmark's own console output and evidence.
- `evaluation_report.md` -- rendered from those `results.json` files: hardware and runtime,
  workload, runs (with links to each `report.txt`), throughput and classroom-summary tables
  (TTFT, TPOT, decode/prefill tok/s, generation time, peak GPU/RAM) with a model-by-task
  comparison on `paged_min`, the speculative off-vs-on table (method, decode and total-call
  speedup, acceptance length, pass cost, TTFT, whether the greedy output stayed identical),
  retrieval accuracy per task and generation fact coverage plus speculative fidelity against
  `paged_min`,
  and every failed, partial or not-run case.

The full plan is 12 invocations (135 cases: 105 throughput, 15 classroom, 15 accuracy) and
runs for many hours on an iGPU: the 512-token decodes of the 14 speculative profiles and the
27B dense model dominate; `--spec-k` or `--models` shortens it. Start with `--quick`, then run the full plan, or a subset
with `--models` / `--suites`, when the machine is otherwise idle (memory is sampled
system-wide). On an iGPU the GPU allocations are system memory: on an idle 64 GiB Arc 140T
machine, Qwen3.6-35B-A3B with the DFlash drafter peaked at about 38 GB committed. The manifest
and every `results.json` are checkpointed as the sweep runs, so if the evaluation process is
ended before it writes `evaluation_report.md`, `--report <evaluation dir>` renders it from what
was recorded.

## Configuration reference

The YAML is safe-loaded and validated strictly: unknown keys, duplicate keys, booleans used as
integers, non-finite numbers and malformed mappings are errors (exit 2). Precedence is CLI >
YAML > tool default; resolved values are never written back.

| Key | Rules |
| --- | --- |
| `model` | `provider: openvino`, `device` (`CPU`, `GPU`, `GPU.1`), `weight_format`, `models_base_path`, optional `model_dirs` |
| `benchmark.models`, `context_tokens` | Unique names; non-negative integers (0 = standalone task prompt, positive = total rendered input tokens) |
| `output_tokens`, `warmup`, `iterations` | >= 2, >= 0, >= 1. Zero warm-up or one iteration is labelled a smoke test |
| `throughput_task` | One or more of `summary_2s` (default), `classroom_summary`, `code`, `math`, `chat`. Summaries skip context 0 |
| `iteration_gap_sec` | Untimed idle before each measured generation (default 0 = sustained); must be below `timeout_sec` |
| `timeout_sec` | Progress timeout (no event for this long), not a total case limit |
| `max_system_memory_pct`, `gpu_memory_budget_gb` | Host RAM budget (default 80); GPU budget in GiB, required for GPU runs; validated when present but not enforced on CPU |
| `cache_dir`, `output_dir`, `record_text` | Compiled-blob cache (optional), evidence base directory, opt-in persistence of synthetic answers (default false) |
| `profiles[]` | `name`, `ov`, `scheduler`, optional `dflash` or `eagle3` (at most one); any other key (e.g. `mtp`, which is not supported) is an error |
| `profile.scheduler` | Empty = stateful pipeline. Any key = paged, which requires positive `max_num_batched_tokens`, `max_num_seqs` and `enable_prefix_caching: false`. `cache_size`: omitted (runtime-managed), whole GiB, or `auto` |
| `profile.dflash`, `profile.eagle3` | `model` (local draft IR directory), `num_assistant_tokens` >= 1, `enabled` (the draft runs on the target's device; any other key is an error); needs a paged `scheduler`, and `num_linear_attention_blocks >= max_num_seqs + k + 1` when set. The section name is the strategy |
| `accuracy` | Read only with `--accuracy`: `suites`, `baseline_profile` (optional; when set it must be one of the profiles being run -- without it there are no deltas and the generation suite reports only Coverage), `seed`, `embedding_model`, and per-suite `tasks`, `depths`, `samples`, `output_tokens`, `options` |
| `speculative_reference` | Published figures printed beside the window sweep; never ranked |

Overrides: `--pipeline-config KEY=VALUE` (strings into `ov`), `--scheduler-config KEY=VALUE`
(booleans/numbers converted), `KEY=` removes a key; validation runs afterwards and a pipeline
change is announced (moving a stateful profile to paged needs all three required scheduler keys). `--assistant-tokens K` changes only profiles that already
speculate. A requested speculative case either runs that mechanism or fails; it never falls
back to plain generation.

`classroom_summary` is the only task that reads application settings: it safe-loads
`config.yaml` for `app.language` and `models.summarizer.mode`, resolves the template with
`load_prompt("summarizer", language, mode)`, renders with thinking disabled and applies
`/no_think` exactly where the application's renderer does. Content is synthetic; an
unresolvable template is a configuration error. Its answer needs about 1024 output tokens, so
no config's default run includes it (512-token ceiling, as in the reference) -- run it with
`--throughput-task classroom_summary --output-tokens 1024` on any config, or through the
evaluation's `classroom` suite.

Because it follows `config.yaml`, the same task name is a different workload when
`app.language` changes: a `zh` summary accepts far fewer DFlash tokens than an `en` one
(Qwen3.6-35B-A3B on the Arc B390 at k=5: AL 2.35, 0.97x for `zh`, against AL 3.32, 1.34x for
`en`). The resolved language and mode are recorded on every case as `task_variant` and shown
as `classroom_summary (<language>, <mode>)` in the plan, `report.txt` and the evaluation
report, and are part of the task hash, so cases of different languages are never paired.
Check it before comparing runs.

### Cache pools

The preflight estimate covers growing KV for full-attention layers (top-level or
`text_config`), context plus decode ceiling (plus `k + 1` verification tokens), fixed
linear-attention state times reserved rows, and the DFlash draft's KV. Any unknown
part makes the estimate unavailable (context 0 always is). A fixed pool below the estimate is
a case error. `auto` becomes `ceil(max(2, 1.3 x estimate))` and must fit
`floor(budget - weights - 8)`; an infeasible or unknown `auto`, `auto` on CPU, and `auto` on
DFlash (no qualified factor) are case errors. The pool is never clamped and `auto` is never
silently removed.

## How a run works

Cases run serially, each in a fresh `spawn` child that loads the local tokenizer
(`local_files_only`, no remote code) and pipeline once. Throughput builds one exactly sized
prompt (configured, HF and OpenVINO counts must agree) and reuses it for the warm-up (full
ceiling) and measured iterations, each with a fresh greedy request. Accuracy runs one prompt and
decode budget per probe (16-token size tolerance); an unbuildable probe becomes a skipped
record, listed in `PROBE DETAIL` and accounted for: as in the reference it does not demote
the case, and the rates are over the scored probes. Before each generate the parent opens a memory window and acknowledges `ready`; after
each record it closes the window, checkpoints and acknowledges `recorded`. A timeout (no
progress), crash or Ctrl-C terminates only the owned child, escalates to kill, and refuses
further cases if death cannot be confirmed. Scoring runs in the parent after the child exits;
optional who_what_benchmark similarity is filled at every report render, as in the reference,
so an interrupted run still reports it for the cases it finished; the embedding model loads
offline (a missing package or model makes only that column unavailable).

## Evidence

Each run creates `<output_dir>/<UTC-timestamp>-<suffix>/` (output directories are gitignored)
with:

- `results.json` -- authoritative, `schema_version: 1`: run ID and revision, tool commit,
  times, completion, redacted resolved settings and source-config hash, environment and
  package versions, artifact identities (XML/JSON hashes, weight sizes), plan with case
  states, counts, warnings, and per case the effective settings, cache provenance, status and
  flags, structured error (category, stage, milestone, phase), raw iteration/probe records
  with per-field timing sources and memory windows, aggregates and accuracy summaries.
- `report.txt` -- rendered from the same snapshot: run header, `CASES` (the plan grid by
  case ID), `SPEED AND RESOURCES`,
  `SPECULATIVE WINDOW SWEEP`, `RETRIEVAL ACCURACY`, `GENERATION FIDELITY`, `PROBE DETAIL`,
  and failures/partial coverage/warnings.

Both are replaced atomically before trials, after every record and case, and in `finally`.
Paths inside the Smart Classroom root are shown relative to it; anything else as
`external:<id>`. Generated and planted text is persisted only with `record_text: true`.

### Reading results

- Latency in ms, durations in s, throughput in tokens/s, memory in GiB. Unavailable values are
  `--` / null, never zero.
- TPOT excludes the first token; `latency` includes prefill; `e2e_throughput` counts input
  and output tokens. Aggregates are p50 with mean, nearest-rank p95, min/max; fewer than 20
  samples carry a small-sample note. Decode tok/s is 1000 / median TPOT.
- AL (acceptance length) excludes the prefill pass; the accepted % is `(AL - 1) / k` and is
  labelled `estimated` when not measured by the runtime. Rank windows by AL against pass cost.
- Speedups compare a speculative case only with a plain case sharing artifact, prompt hash,
  task, device, plan and fully resolved settings; decode, total-call and steady speedups are
  separate, and differing auto pools are marked confounded. Output forks are warnings.
- Memory is system-wide and sampled (default 0.5 s); windows include IPC/sampling delay.

| Status | Meaning |
| --- | --- |
| `ok` | All planned work completed with required telemetry inside budgets |
| `memory_limit`, `gpu_memory_limit` | Completed, but a measured budget was exceeded |
| `measurement_error` | Completed, but required telemetry or integrity (e.g. runtime input count) is missing |
| `partial` | Only some iterations completed, or accuracy probes that were neither scored nor skipped |
| `oom`, `gpu_abort`, `unsupported` | Allocation/scheduler failure, device command abort, unsupported property or capability |
| `timeout`, `crashed` | No progress within `timeout_sec`, or the child died without a result |
| `no_output` | No measured output (warm-up does not count) |
| `load_error`, `error`, `missing_ir` | Other load/generation/preflight/orchestration failure, or absent artifacts |

Only `ok` cases are ranked, and only an `ok` baseline case yields deltas and fidelity.
Accuracy aggregates, deltas and similarity are rounded to 4 decimals, and planted values are
seeded exactly as in the reference implementation, so the same seed plants the same facts.
Synthetic retrieval scores are not published RULER scores, and fidelity measures drift from
the baseline, not absolute quality.

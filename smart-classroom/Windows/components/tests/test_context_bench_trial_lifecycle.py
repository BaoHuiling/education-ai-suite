# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Case lifecycle with fake pipelines, queues, processes, samplers and clocks.

Covers the package location and launcher, the child's load-once/prompt-once contract and
milestone resets, the ready/recorded acknowledgement protocol and its checkpoint ordering,
recovery of results that race the child's exit, timeout and Ctrl-C cleanup, sampler window
separation, atomic evidence and the CLI exit codes.
"""

import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from unittest import mock

from components.context_bench import benchmark, evidence, metrics, settings, supervisor, trial_runner

SC_ROOT = Path(__file__).resolve().parents[2]
FAKE_MEM = {"ram_gb": 10.0, "ram_pct": 15.6, "available_ram_gb": 54.0, "gpu_gb": 1.0}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class WordTokenizer:
    def __init__(self):
        self._words, self._ids = [], {}

    def encode(self, text, add_special_tokens=True):
        out = []
        for word in text.split():
            if word not in self._ids:
                self._ids[word] = len(self._words)
                self._words.append(word)
            out.append(self._ids[word])
        return out

    def decode(self, ids, skip_special_tokens=True):
        return " ".join(self._words[i] for i in ids)

    def apply_chat_template(self, messages, tokenize=False, **kwargs):
        return " ".join(f"<{m['role']}> {m['content']}" for m in messages) + " <assistant>"


def fake_genai():
    class GenerationConfig:
        pass

    return types.SimpleNamespace(GenerationConfig=GenerationConfig,
                                 StreamingStatus=types.SimpleNamespace(RUNNING="running"))


class FakePipe:
    """Streams `tokens` tokens per call; optionally fails on one call before/after the first."""

    def __init__(self, tokenizer, tokens=5, fail_on=None, fail_after_first_token=False):
        self.tokenizer = tokenizer
        self.tokens = tokens
        self.fail_on = fail_on
        self.fail_after_first_token = fail_after_first_token
        self.calls = []

    def get_tokenizer(self):
        tok = self.tokenizer
        return SimpleNamespace(encode=lambda text: SimpleNamespace(
            input_ids=SimpleNamespace(shape=(1, len(tok.encode(text))))))

    def generate(self, pipeline_input, generation_config, streamer):
        self.calls.append((pipeline_input, generation_config, generation_config.max_new_tokens))
        index = len(self.calls) - 1
        if index == self.fail_on and not self.fail_after_first_token:
            raise RuntimeError("Out of GPU resources during prefill")
        count = min(self.tokens, generation_config.max_new_tokens)
        for position in range(count):
            streamer("w")
            if index == self.fail_on and self.fail_after_first_token and position == 0:
                raise RuntimeError("clWaitForEvents failed")
        return SimpleNamespace(texts=[" ".join(["w"] * count)], tokens=[[0] * count])


def _case(mode="throughput", **values):
    case = {"case_id": "c001", "mode": mode, "model_dir": "unused", "device": "GPU",
            "context_tokens": 600, "output_tokens": 16, "warmup": 1, "iterations": 3,
            "iteration_gap_sec": 0.0, "timeout_sec": 5, "ov_config": {}, "scheduler_config": {},
            "mtp": {"enabled": False}, "throughput_task": None, "probes": None}
    case.update(values)
    return case


class _Loaders:
    """Patches the child's runtime loaders with fakes and counts loads."""

    def __init__(self, pipe, tokenizer):
        self.pipe, self.tokenizer, self.loads = pipe, tokenizer, 0
        self._patches = []

    def _load_pipeline(self, *args, **kwargs):
        self.loads += 1
        return self.pipe, True

    def __enter__(self):
        self._patches = [
            mock.patch.object(trial_runner, "_load_tokenizer", lambda _d: self.tokenizer),
            mock.patch.object(trial_runner, "_load_pipeline", self._load_pipeline),
            mock.patch.dict(sys.modules, {"openvino_genai": fake_genai()}),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()


def _events(results: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(results.get_nowait())
        except Empty:
            return out


# ---------------------------------------------------------------------------
# Location, resolution, launcher
# ---------------------------------------------------------------------------
class TestPackageLocation(unittest.TestCase):
    def test_imports_are_lightweight(self):
        code = ("import sys; import components.context_bench.benchmark, components.context_bench.report; "
                "print(sorted(m for m in ('openvino', 'openvino_genai', 'transformers', 'numpy', "
                "'utils.config_loader') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=SC_ROOT, capture_output=True,
                             text=True, timeout=60, check=True).stdout.strip()
        self.assertEqual(out, "[]")

    def test_root_config_and_environment_resolution(self):
        self.assertEqual(settings.SC_ROOT, SC_ROOT)
        self.assertEqual(settings.TOOL_DIR, SC_ROOT / "components" / "context_bench")
        self.assertEqual(settings.DEFAULT_CONFIG_PATH, settings.TOOL_DIR / "config_qwen3.6_35b.yaml")
        self.assertTrue(settings.DEFAULT_CONFIG_PATH.is_file())
        self.assertEqual(benchmark._BACKEND_VENV_PYTHON,
                         SC_ROOT.parent / "smartclassroom" / "Scripts" / "python.exe")
        self.assertEqual(settings.resolve_path("models/x"), SC_ROOT / "models" / "x")
        self.assertEqual(settings._parse_args([]).config, str(settings.DEFAULT_CONFIG_PATH))

    def test_relative_config_resolves_against_the_callers_directory(self):
        with tempfile.TemporaryDirectory() as caller, mock.patch.object(Path, "cwd", return_value=Path(caller)):
            self.assertEqual(settings._config_path(SimpleNamespace(config="b.yaml")), Path(caller) / "b.yaml")

    def test_launcher_contract_in_source(self):
        text = (settings.TOOL_DIR / "run_benchmark.ps1").read_text(encoding="utf-8")
        for required in ("Set-StrictMode", "$ErrorActionPreference = 'Stop'", "'..\\..'",
                         "'smartclassroom'", "-m components.context_bench.benchmark",
                         "$LASTEXITCODE", "exit $ExitCode", "Push-Location", "Resolve-CallerPath"):
            self.assertIn(required, text)
        self.assertIn("start-desktop-app.ps1", text)
        # DEPRECATED(webui): drop after 2027.0.0, with setup-smart-classroom.ps1.
        self.assertIn("setup-smart-classroom.ps1", text)
        self.assertIn("-Scope Process", text)

    def _shells(self):
        return [shell for shell in ("powershell", "pwsh") if shutil.which(shell)]

    def test_launcher_forwards_arguments_resolves_config_and_propagates_exit_codes(self):
        shells = self._shells()
        if not shells:
            self.skipTest("PowerShell is not available")
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            package = root / "sc" / "components" / "context_bench"
            package.mkdir(parents=True)
            source = (settings.TOOL_DIR / "run_benchmark.ps1").read_text(encoding="utf-8")
            # Point the launcher at this interpreter instead of the backend venv; all argument
            # handling, directory changes and exit propagation stay the shipped code.
            source = source.replace("$VenvPython = Join-Path $VenvPath 'Scripts\\python.exe'",
                                    f"$VenvPython = '{sys.executable}'")
            (package / "run_benchmark.ps1").write_text(source, encoding="utf-8")
            (package / "benchmark.py").write_text(
                "import json, os, sys\n"
                "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}))\n"
                "sys.exit(int(os.environ.get('FAKE_EXIT', '0')))\n", encoding="utf-8")
            caller = root / "caller"
            caller.mkdir()
            for shell in shells:
                for code in (0, 1, 2, 130):
                    with self.subTest(shell=shell, code=code):
                        completed = subprocess.run(
                            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(package / "run_benchmark.ps1"), "--config", "rel/x.yaml",
                             "--contexts", "0", "8000", "--accuracy-depths", "0.5", "--list-profiles"],
                            cwd=caller, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "FAKE_EXIT": str(code)},
                        )
                        self.assertEqual(completed.returncode, code, completed.stderr)
                        payload = json.loads(completed.stdout.strip().splitlines()[-1])
                        self.assertEqual(Path(payload["cwd"]).resolve(), (root / "sc").resolve())
                        self.assertEqual(Path(payload["argv"][1]).resolve(), (caller / "rel" / "x.yaml").resolve())
                        self.assertEqual(payload["argv"][2:], ["--contexts", "0", "8000",
                                                                "--accuracy-depths", "0.5", "--list-profiles"])


# ---------------------------------------------------------------------------
# Child execution
# ---------------------------------------------------------------------------
class TestChildThroughput(unittest.TestCase):
    def _execute(self, pipe, tokenizer, **case_values):
        results = queue.Queue()
        channel = trial_runner._Channel("c001", results, None, 5)
        with _Loaders(pipe, tokenizer) as loaders, \
                mock.patch.object(trial_runner, "build_benchmark_prompt",
                                  wraps=trial_runner.build_benchmark_prompt) as builder:
            done = trial_runner.execute_case(_case(**case_values), channel)
        return done, _events(results), loaders, builder

    def test_one_load_one_final_prompt_full_budget_warmup_and_fresh_requests(self):
        tokenizer = WordTokenizer()
        pipe = FakePipe(tokenizer, tokens=5)
        done, events, loaders, builder = self._execute(pipe, tokenizer)
        self.assertIsNone(done["error"])
        self.assertEqual(loaders.loads, 1)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(len(pipe.calls), 4)
        inputs = {id(call[0]) for call in pipe.calls}
        self.assertEqual(len(inputs), 1)                       # the final prompt is reused
        self.assertEqual({call[2] for call in pipe.calls}, {16})  # warm-up uses the full ceiling
        self.assertEqual(len({id(call[1]) for call in pipe.calls}), 4)  # fresh config per call
        self.assertEqual(done["prompt"]["configured_tokens"], 600)
        self.assertEqual(done["prompt"]["hf_tokens"], done["prompt"]["ov_tokens"])
        self.assertEqual([r["warmup"] for r in done["iterations"]], [True, False, False, False])
        names = [e["event"] for e in events]
        self.assertEqual(names[:3], ["device", "loaded", "prompt"])
        self.assertEqual(names.count("iteration_start"), 4)
        self.assertTrue(all(e["case_id"] == "c001" for e in events))
        self.assertEqual(done["stage_reached"], trial_runner.STAGE_DECODED)

    def test_milestones_reset_so_a_later_prefill_failure_is_not_a_decode_failure(self):
        tokenizer = WordTokenizer()
        done, _, _, _ = self._execute(FakePipe(tokenizer, fail_on=2), tokenizer)
        self.assertEqual(done["error"]["category"], "oom")
        self.assertEqual(done["error"]["stage"], "prefill")
        self.assertEqual(done["error"]["milestone"], trial_runner.STAGE_PROMPT_BUILT)
        self.assertEqual(done["error"]["iteration"], 2)
        self.assertEqual(len(done["iterations"]), 2)

    def test_a_failure_after_the_first_token_is_a_decode_failure(self):
        tokenizer = WordTokenizer()
        done, _, _, _ = self._execute(FakePipe(tokenizer, fail_on=1, fail_after_first_token=True), tokenizer)
        self.assertEqual((done["error"]["category"], done["error"]["stage"]), ("gpu_abort", "decode"))

    def test_prompt_size_mismatch_fails_before_the_prompt_milestone(self):
        tokenizer = WordTokenizer()
        pipe = FakePipe(tokenizer)
        pipe.get_tokenizer = lambda: SimpleNamespace(encode=lambda _t: SimpleNamespace(
            input_ids=SimpleNamespace(shape=(1, 599))))
        done, events, _, _ = self._execute(pipe, tokenizer)
        self.assertEqual((done["error"]["category"], done["error"]["stage"]), ("prompt", "prompt"))
        self.assertNotIn("prompt", [e["event"] for e in events])
        self.assertEqual(pipe.calls, [])

    def test_load_failure_reports_the_load_stage_and_never_generates(self):
        tokenizer = WordTokenizer()
        pipe = FakePipe(tokenizer)
        results = queue.Queue()
        with _Loaders(pipe, tokenizer), mock.patch.object(
                trial_runner, "_load_pipeline", side_effect=RuntimeError("unknown property FOO")):
            done = trial_runner.execute_case(_case(), trial_runner._Channel("c001", results, None, 5))
        self.assertFalse(done["load_ok"])
        self.assertEqual((done["error"]["category"], done["error"]["stage"]), ("unsupported", "load"))

    def test_the_gap_precedes_iteration_start_and_never_the_warmup(self):
        tokenizer = WordTokenizer()
        order = []
        results = queue.Queue()
        channel = trial_runner._Channel("c001", results, None, 5)
        original_post = channel.post
        channel.post = lambda event, **p: (order.append(("post", event, p.get("iteration"))),
                                           original_post(event, **p))[-1]
        with _Loaders(FakePipe(tokenizer), tokenizer), mock.patch.object(
                trial_runner.time, "sleep", side_effect=lambda s: order.append(("sleep", s))):
            trial_runner.execute_case(_case(iteration_gap_sec=2.0, iterations=2), channel)
        sleeps = [i for i, item in enumerate(order) if item[0] == "sleep"]
        starts = [i for i, item in enumerate(order) if item[1:2] == ("iteration_start",)]
        self.assertEqual(len(sleeps), 2)  # measured iterations only
        self.assertLess(starts[0], sleeps[0])  # warm-up start comes before any sleep
        self.assertEqual([order[i + 1][1] for i in sleeps], ["iteration_start"] * 2)

    def test_tokens_only_results_are_decoded(self):
        # LLMPipeline given TokenizedInputs returns EncodedResults: tokens, no texts.
        tokenizer = SimpleNamespace(
            decode=lambda ids, skip_special_tokens=True: " ".join(f"t{i}" for i in ids))
        result = SimpleNamespace(tokens=[[1, 2]])
        self.assertEqual(trial_runner.generated_text(result, tokenizer), "t1 t2")
        texts = SimpleNamespace(texts=["hello"], tokens=[[1]])
        self.assertEqual(trial_runner.generated_text(texts, tokenizer), "hello")


class TestChildAccuracy(unittest.TestCase):
    def _probe(self, index, task, inserts, output_tokens=8):
        return {"probe_index": index, "suite": "retrieval", "task": task, "sample": 0, "depth": 0.5,
                "inserts": inserts, "question": "What is the code?", "filler": None,
                "output_tokens": output_tokens, "fixture_id": f"f{index}"}

    def test_unbuildable_probe_is_skipped_and_warmup_uses_the_first_buildable(self):
        tokenizer = WordTokenizer()
        pipe = FakePipe(tokenizer, tokens=50)
        probes = [self._probe(0, "niah_single", [[0.5, " ".join(["huge"] * 5000)]]),
                  self._probe(1, "niah_single", [[0.5, "the code is ABC"]], output_tokens=8),
                  self._probe(2, "qa", [[0.2, "room Halverson"]], output_tokens=4)]
        results = queue.Queue()
        with _Loaders(pipe, tokenizer):
            done = trial_runner.execute_case(_case(mode="accuracy", probes=probes, output_tokens=8),
                                             trial_runner._Channel("c001", results, None, 5))
        self.assertIsNone(done["error"])
        statuses = [(p["probe_index"], p["status"]) for p in done["probes"]]
        self.assertEqual(statuses, [(0, "skipped"), (1, "ok"), (2, "ok")])
        self.assertIn("cannot hold", done["probes"][0]["skip_reason"])
        self.assertEqual(len(pipe.calls), 3)  # one warm-up plus two probes
        self.assertIs(pipe.calls[0][0], pipe.calls[1][0])  # warm-up reused probe 1's prompt
        self.assertEqual([c[2] for c in pipe.calls], [8, 8, 4])  # per-probe decode ceilings
        ok = [p for p in done["probes"] if p["status"] == "ok"]
        self.assertTrue(all(p["potentially_truncated"] for p in ok))
        self.assertEqual(ok[0]["iteration"], 1 + 1)  # warmup + probe_index
        self.assertTrue(all("prediction_text" in p for p in ok))

    def test_no_buildable_probe_produces_no_generation(self):
        tokenizer = WordTokenizer()
        pipe = FakePipe(tokenizer)
        probes = [self._probe(0, "niah_single", [[0.5, " ".join(["huge"] * 5000)]])]
        with _Loaders(pipe, tokenizer):
            done = trial_runner.execute_case(_case(mode="accuracy", probes=probes),
                                             trial_runner._Channel("c001", queue.Queue(), None, 5))
        self.assertEqual(pipe.calls, [])
        self.assertEqual(done["probes"][0]["status"], "skipped")


class TestChildExit(unittest.TestCase):
    class RecordingQueue:
        def __init__(self, put_error=None):
            self.calls, self._error = [], put_error

        def put(self, message):
            self.calls.append(("put", message))
            if self._error:
                raise self._error

        def close(self):
            self.calls.append(("close", None))

        def join_thread(self):
            self.calls.append(("join_thread", None))

    def test_result_is_flushed_then_the_process_exits(self):
        q = self.RecordingQueue()
        with mock.patch.object(trial_runner.os, "_exit", side_effect=lambda code: q.calls.append(("exit", code))):
            trial_runner._post_and_exit(q, {"event": "done"})
        self.assertEqual(q.calls, [("put", {"event": "done"}), ("close", None), ("join_thread", None), ("exit", 0)])

    def test_exits_even_if_reporting_fails(self):
        q = self.RecordingQueue(put_error=OSError("pipe closed"))
        with mock.patch.object(trial_runner.os, "_exit") as fake_exit, \
                mock.patch.object(trial_runner.traceback, "print_exc"):
            trial_runner._post_and_exit(q, {"event": "done"})
        fake_exit.assert_called_once_with(0)

    def test_run_case_reports_once_even_on_an_unexpected_failure(self):
        posted = []
        with mock.patch.object(trial_runner, "execute_case", side_effect=KeyboardInterrupt), \
                mock.patch.object(trial_runner, "_post_and_exit", lambda q, m: posted.append(m)):
            trial_runner.run_case(_case(), queue.Queue(), queue.Queue())
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["event"], "done")
        self.assertEqual(posted[0]["case_id"], "c001")
        self.assertIsNotNone(posted[0]["error"])

    def test_the_child_never_destroys_the_pipeline(self):
        import inspect

        source = "\n".join(line for line in inspect.getsource(trial_runner).splitlines()
                           if not line.strip().startswith("#"))
        self.assertNotIn("del pipe", source)
        self.assertNotIn("import gc", source)
        self.assertNotIn("gc.collect", source)
        self.assertNotIn("_ensure_dflash_target_metadata", source)
        self.assertNotIn("trust_remote_code=True", source)
        self.assertIn("local_files_only=True", source)

    def test_ack_wait_is_bounded_and_ignores_stale_acknowledgements(self):
        control = queue.Queue()
        channel = trial_runner._Channel("c001", queue.Queue(), control, 0.05)
        control.put({"case_id": "other", "iteration": 1, "ack": "ready"})
        control.put({"case_id": "c001", "iteration": 0, "ack": "ready"})
        with self.assertRaisesRegex(trial_runner.OrchestrationError, "no 'ready' acknowledgement"):
            channel.wait_ack(1, "ready")
        control.put({"case_id": "c001", "iteration": 1, "ack": "recorded"})
        channel.wait_ack(1, "recorded")


# ---------------------------------------------------------------------------
# Parent supervision with an in-process (threaded) child
# ---------------------------------------------------------------------------
class ThreadProcess:
    def __init__(self, target, args):
        self._thread = threading.Thread(target=target, args=args, daemon=True)
        self.exitcode = None
        self.terminated = False

    def start(self):
        self._thread.start()

    def is_alive(self):
        return self._thread.is_alive()

    def join(self, timeout=None):
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self.exitcode = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass


class ThreadContext:
    def __init__(self):
        self.queues = []
        self.process = None

    def Queue(self):  # noqa: N802 - mirrors multiprocessing's context API
        self.queues.append(queue.Queue())
        return self.queues[-1]

    def Process(self, target=None, args=()):  # noqa: N802
        self.process = ThreadProcess(target, args)
        return self.process


class _ParentHarness(unittest.TestCase):
    def _run(self, ctx, spec=None, **kwargs):
        with mock.patch.object(supervisor.multiprocessing, "get_context", return_value=ctx), \
                mock.patch.object(supervisor, "_read_mem", side_effect=lambda include_gpu=True: dict(FAKE_MEM)):
            return supervisor._run_case_subprocess(
                spec or _case(), kwargs.pop("timeout_sec", 5), sample_interval=0.01,
                poll_interval=0.01, drain_timeout=0.2, join_timeout=0.5, kill_timeout=0.1, **kwargs)


class TestAcknowledgementProtocol(_ParentHarness):
    def test_records_are_windowed_checkpointed_and_acknowledged_in_order(self):
        tokenizer = WordTokenizer()
        pipe = FakePipe(tokenizer)
        timeline = []

        def on_record(kind, record):
            time.sleep(0.05)  # a slow checkpoint must not overlap the next iteration
            timeline.append(("checkpointed", record["iteration"], time.monotonic()))

        original = pipe.generate

        def generate(*args, **kwargs):
            timeline.append(("generate", len(pipe.calls), time.monotonic()))
            return original(*args, **kwargs)

        pipe.generate = generate
        ctx = ThreadContext()
        with _Loaders(pipe, tokenizer), \
                mock.patch.object(trial_runner, "_post_and_exit", lambda q, m: q.put(m)):
            result = self._run(ctx, on_record=on_record)
        self.assertIsNone(result["error"])
        self.assertTrue(result["cleanup_confirmed"])
        self.assertEqual([r["iteration"] for r in result["iterations"]], [0, 1, 2, 3])
        for record in result["iterations"]:
            self.assertEqual(record["peak_ram_gb"], FAKE_MEM["ram_gb"])
            self.assertEqual(record["memory_window"]["boundary"], supervisor.WINDOW_BOUNDARY)
        # Each generate after the first starts only after the previous record was checkpointed.
        for index in range(1, 4):
            checkpoint = next(t for kind, i, t in timeline if kind == "checkpointed" and i == index - 1)
            started = next(t for kind, i, t in timeline if kind == "generate" and i == index)
            self.assertLessEqual(checkpoint, started)
        control = ctx.queues[1]
        # The child consumed every ack; the parent sent ready+recorded for each iteration.
        self.assertTrue(control.empty())

    def test_done_records_are_merged_by_identity_without_duplicates(self):
        ctx = _ScriptedContext([
            {"event": "loaded", "load_time_s": 1.0},
            {"event": "iteration_start", "iteration": 0, "warmup": False},
            _iteration(0),
            {"event": "done", "load_ok": True, "error": None,
             "iterations": [_iteration(0), _iteration(1)]},
        ])
        result = self._run(ctx)
        self.assertEqual([r["iteration"] for r in result["iterations"]], [0, 1])
        self.assertIsNotNone(result["iterations"][0]["memory_window"])
        self.assertIsNone(result["iterations"][1]["memory_window"])  # only in `done`

    def test_events_from_another_case_are_ignored(self):
        ctx = _ScriptedContext([{"event": "iteration", "case_id": "other", "iteration": 0,
                                 "warmup": False, "output_size": 5}], alive=True)
        result = self._run(ctx, timeout_sec=0.2)
        # The foreign event neither counts as a record nor resets the progress deadline.
        self.assertEqual(result["iterations"], [])
        self.assertEqual(result["invalid_events"], 1)
        self.assertEqual(result["error"]["category"], "timeout")


def _iteration(index, warmup=False):
    return {"event": "iteration", **metrics.iteration_record(index, 8000, 64, 2.0, 1000.0, warmup=warmup),
            "status": "ok"}


class _ScriptedProcess:
    def __init__(self, alive=False, dies_on_terminate=True, exitcode=0):
        self._alive, self._dies = alive, dies_on_terminate
        self.exitcode = exitcode
        self.terminated = self.killed = False

    def start(self):
        pass

    def is_alive(self):
        return self._alive

    def terminate(self):
        self.terminated = True
        if self._dies:
            self._alive = False

    def kill(self):
        self.killed = True

    def join(self, timeout=None):
        pass


class _ScriptedQueue:
    """Empty on the first poll (the race), then the scripted messages, then Empty."""

    def __init__(self, messages, raise_on_get=None):
        self._messages = [dict(m, case_id=m.get("case_id", "c001")) for m in messages]
        self._polled = False
        self._raise = raise_on_get
        self.sent = []

    def get(self, timeout=None):
        if self._raise is not None:
            error, self._raise = self._raise, None
            raise error
        if not self._polled:
            self._polled = True
            raise Empty
        if not self._messages:
            if timeout:
                time.sleep(min(timeout, 0.01))
            raise Empty
        return self._messages.pop(0)

    def put(self, message):
        self.sent.append(message)


class _ScriptedContext:
    def __init__(self, messages, alive=False, dies_on_terminate=True, exitcode=0, raise_on_get=None):
        self.result_queue = _ScriptedQueue(messages, raise_on_get)
        self.control_queue = _ScriptedQueue([])
        self.process = _ScriptedProcess(alive, dies_on_terminate, exitcode)
        self._made = 0

    def Queue(self):  # noqa: N802
        self._made += 1
        return self.result_queue if self._made == 1 else self.control_queue

    def Process(self, target=None, args=()):  # noqa: N802
        return self.process


class TestExitRaceAndFailures(_ParentHarness):
    def test_a_completed_case_racing_the_child_exit_is_not_a_crash(self):
        ctx = _ScriptedContext([
            {"event": "device", "gpu_budget_gb": 33.6},
            {"event": "loaded", "load_time_s": 13.1},
            {"event": "prompt", "ov_tokens": 8000, "prompt_sha256": "p"},
            {"event": "iteration_start", "iteration": 0, "warmup": True},
            _iteration(0, warmup=True),
            {"event": "done", "load_ok": True, "error": None, "iterations": [],
             "stage_reached": trial_runner.STAGE_DECODED},
        ])
        result = self._run(ctx)
        self.assertIsNone(result["error"])
        self.assertEqual(result["gpu_budget_driver_gb"], 33.6)
        self.assertEqual(len(result["iterations"]), 1)
        # Post-mortem events never fabricate a post-load memory delta.
        self.assertIsNone(result["post_load_peak_ram_gb"])

    def test_partial_records_survive_a_native_crash_with_the_last_milestone(self):
        ctx = _ScriptedContext([
            {"event": "loaded", "load_time_s": 1.0},
            {"event": "iteration_start", "iteration": 0, "warmup": False}, _iteration(0),
            {"event": "iteration_start", "iteration": 1, "warmup": False},
            {"event": "prefilled", "iteration": 1},
        ], exitcode=3221226505)
        result = self._run(ctx)
        self.assertEqual(len(result["iterations"]), 1)
        self.assertEqual(result["error"]["category"], "crashed")
        self.assertEqual(result["error"]["stage"], "decode")
        self.assertIn("0xC0000409", result["error"]["message"])
        self.assertEqual(result["error"]["exit_code"], 3221226505)

    def test_timeout_terminates_only_the_owned_child_and_confirms_death(self):
        ctx = _ScriptedContext([{"event": "loaded", "load_time_s": 1.0}], alive=True)
        result = self._run(ctx, timeout_sec=0.2)
        self.assertTrue(ctx.process.terminated)
        self.assertEqual(result["error"]["category"], "timeout")
        self.assertEqual(result["error"]["stage"], "prompt")
        self.assertTrue(result["cleanup_confirmed"])

    def test_unconfirmed_death_refuses_further_cases(self):
        ctx = _ScriptedContext([], alive=True, dies_on_terminate=False)
        with self.assertRaises(supervisor.ChildCleanupError) as caught:
            self._run(ctx, timeout_sec=0.1)
        self.assertTrue(ctx.process.terminated and ctx.process.killed)
        self.assertFalse(caught.exception.result["cleanup_confirmed"])

    def test_ctrl_c_cleans_up_the_child_then_reports_the_partial_result(self):
        ctx = _ScriptedContext([], alive=True, raise_on_get=KeyboardInterrupt())
        with self.assertRaises(supervisor.CaseInterrupted) as caught:
            self._run(ctx)
        self.assertTrue(ctx.process.terminated)
        self.assertEqual(caught.exception.result["error"]["category"], "interrupted")


class TestRealSpawnedChild(unittest.TestCase):
    """The real `spawn` context, queues and cleanup, with a case that cannot load here."""

    def test_a_load_failure_is_reported_by_the_child_and_its_exit_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            spec = _case(device="CPU", model_dir=str(Path(root, "absent")), timeout_sec=60)
            result = supervisor._run_case_subprocess(spec, 60, include_gpu=False, join_timeout=30)
        self.assertFalse(result["load_ok"])
        self.assertEqual(result["error"]["stage"], "load")
        self.assertEqual(result["stage_reached"], trial_runner.STAGE_START)
        self.assertTrue(result["cleanup_confirmed"])
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn(root, json.dumps(result["error"]))
        self.assertIsNone(result["peak_gpu_gb"])  # CPU run: no GPU telemetry


class TestSampler(unittest.TestCase):
    def test_window_reset_keeps_case_peaks_and_reports_its_own_values(self):
        clock = iter(range(100)).__next__
        with mock.patch.object(supervisor, "_read_mem", return_value=dict(FAKE_MEM)):
            sampler = supervisor._MemorySampler(interval=3600, clock=lambda: float(clock()))
        sampler._observe({"ram_gb": 60.0, "gpu_gb": 50.0, "ram_pct": 95.0, "available_ram_gb": 1.0})
        quiet = {"ram_gb": 20.0, "gpu_gb": 10.0, "ram_pct": 30.0, "available_ram_gb": 40.0}
        with mock.patch.object(supervisor, "_read_mem", return_value=quiet):
            sampler.reset_window()
        sampler._observe(quiet)
        window = sampler.window()
        self.assertEqual((window["peak_ram_gb"], window["peak_gpu_gb"]), (20.0, 10.0))
        self.assertEqual((window["mean_ram_gb"], window["mean_gpu_gb"]), (20.0, 10.0))
        self.assertEqual((sampler.peak_ram, sampler.peak_gpu, sampler.min_available_ram), (60.0, 50.0, 1.0))
        self.assertEqual(window["memory_window"]["samples"], 2)

    def test_cpu_runs_never_read_gpu_counters(self):
        calls = []
        with mock.patch.object(supervisor, "_read_mem", side_effect=lambda include_gpu=True: calls.append(include_gpu) or dict(FAKE_MEM)):
            sampler = supervisor._MemorySampler(interval=3600, include_gpu=False)
            sampler.reset_window()
        self.assertEqual(set(calls), {False})
        self.assertIsNone(sampler.window()["peak_gpu_gb"])

    def test_time_weighted_mean(self):
        mean = supervisor._TimeWeightedMean(10.0, 0.0)
        mean.observe(20.0, 1.0)
        self.assertAlmostEqual(mean.mean(10.0), 19.0)
        gap = supervisor._TimeWeightedMean(None, 0.0)
        gap.observe(30.0, 5.0)
        self.assertAlmostEqual(gap.mean(10.0), 30.0)
        self.assertIsNone(supervisor._TimeWeightedMean(None, 0.0).mean(10.0))

    def test_memory_settle_waits_then_gives_up(self):
        readings = [{"ram_gb": 55.0, "gpu_gb": 40.0}, {"ram_gb": 10.4, "gpu_gb": 1.2}]
        with mock.patch.object(supervisor, "_read_mem", side_effect=lambda include_gpu=True: readings.pop(0)), \
                mock.patch.object(supervisor.time, "sleep"):
            self.assertTrue(supervisor._wait_for_memory_settle({"ram_gb": 10.0, "gpu_gb": 1.0}))
        with mock.patch.object(supervisor, "_read_mem", return_value={"ram_gb": 40.0, "gpu_gb": 20.0}), \
                mock.patch.object(supervisor.time, "sleep"):
            self.assertFalse(supervisor._wait_for_memory_settle({"ram_gb": 10.0, "gpu_gb": 1.0}, timeout_sec=0.01))


# ---------------------------------------------------------------------------
# Evidence and exit codes
# ---------------------------------------------------------------------------
def _write_model(directory: Path) -> Path:
    directory.mkdir(parents=True)
    for name in ("openvino_model", "openvino_tokenizer", "openvino_detokenizer"):
        (directory / f"{name}.xml").write_text("<net><layers/></net>", encoding="utf-8")
        (directory / f"{name}.bin").write_bytes(b"\0" * 64)
    (directory / "config.json").write_text(json.dumps(
        {"num_hidden_layers": 4, "num_key_value_heads": 2, "head_dim": 64}), encoding="utf-8")
    (directory / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    return directory


class TestEvidenceAndExitCodes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.model = _write_model(self.root / "model")
        self.output = self.root / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def _settings(self, **benchmark_values):
        raw = {
            "model": {"provider": "openvino", "device": "CPU", "weight_format": "fp16",
                      "models_base_path": "models", "model_dirs": {"M": str(self.model)}},
            "benchmark": {"models": ["M"], "context_tokens": [600], "output_tokens": 16, "warmup": 1,
                          "iterations": 2, "timeout_sec": 30, "output_dir": str(self.output),
                          **benchmark_values},
            "profiles": [{"name": "stateful", "ov": {}}],
        }
        resolved = settings._resolve_settings(raw, settings._parse_args([]), "C:/somewhere/x.yaml", "ab" * 32)
        return resolved, settings.plan_run(resolved)

    def _fake_subprocess(self, iterations=3, error=None, interrupt=False, revisions=None):
        def run(spec, timeout_sec, include_gpu=True, on_record=None, **_kwargs):
            records = []
            for index in range(iterations):
                record = {**metrics.iteration_record(index, 600, 5, 0.3, 100.0, warmup=index == 0),
                          "status": "ok", "case_id": spec["case_id"], "iteration": index,
                          "output_text": "synthetic answer", "prompt_sha256": "p",
                          "peak_ram_gb": 10.0, "mean_ram_gb": 10.0, "memory_window": {"samples": 2}}
                records.append(record)
                on_record("iteration", record)
                if revisions is not None:
                    revisions.append(json.loads(next(self.output.glob("*/results.json")).read_text())["revision"])
            result = {"case_id": spec["case_id"], "load_ok": True, "load_time_s": 1.0,
                      "prompt": {"prompt_sha256": "p", "ov_tokens": 600, "hf_tokens": 600,
                                 "configured_tokens": 600},
                      "prompt_tokens": 600, "stage_reached": "decoded", "error": error,
                      "iterations": records, "probes": [], "cleanup_confirmed": True,
                      "memory_baseline": dict(FAKE_MEM), "peak_ram_gb": 12.0, "peak_ram_pct": 20.0,
                      "peak_gpu_gb": None, "device": {"device_name": "Fake CPU"}}
            if interrupt:
                result["error"] = {"category": "interrupted", "stage": "decode", "message": "Ctrl-C"}
                raise supervisor.CaseInterrupted(result)
            return result
        return run

    def _execute(self, resolved, planned, fake):
        with mock.patch.object(benchmark, "_run_case_subprocess", fake), \
                mock.patch.object(benchmark, "_wait_for_memory_settle", return_value=True), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            return benchmark.execute(resolved, planned, evidence.PathRedactor(SC_ROOT))

    def _evidence(self):
        run_dir = next(self.output.iterdir())
        return run_dir, json.loads((run_dir / "results.json").read_text(encoding="utf-8")), \
            (run_dir / "report.txt").read_text(encoding="utf-8")

    def test_completed_ok_run_writes_versioned_atomic_evidence_and_exits_zero(self):
        resolved, planned = self._settings()
        revisions = []
        self.assertEqual(self._execute(resolved, planned, self._fake_subprocess(revisions=revisions)), 0)
        run_dir, results, text = self._evidence()
        self.assertRegex(run_dir.name, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertEqual(results["schema_version"], 1)
        self.assertEqual(results["run_id"], run_dir.name)
        self.assertTrue(results["completed"])
        self.assertEqual(results["exit_code"], 0)
        self.assertIn(f"Run       : {results['run_id']} revision {results['revision']}", text)
        self.assertEqual(revisions, sorted(revisions))
        self.assertGreaterEqual(revisions[0], 2)  # checkpointed before trials, then per record
        self.assertEqual([p.name for p in run_dir.iterdir()], ["report.txt", "results.json"])
        case = results["cases"][0]
        self.assertEqual(case["status"], "ok")
        self.assertEqual(case["completed_iterations"], 2)
        self.assertNotIn("output_text", json.dumps(results))  # record_text: false
        self.assertTrue(results["settings"]["config_path"].startswith("external:"))
        self.assertTrue(results["settings"]["model_dirs"]["M"].startswith("external:"))
        self.assertNotIn(str(self.root), json.dumps(results))
        self.assertEqual(results["environment"]["device_names"], ["Fake CPU"])

    def test_record_text_opt_in_keeps_synthetic_answers(self):
        resolved, planned = self._settings(record_text=True)
        self._execute(resolved, planned, self._fake_subprocess())
        _, results, _ = self._evidence()
        self.assertEqual(results["cases"][0]["iterations"][1]["output_text"], "synthetic answer")

    def test_partial_coverage_exits_one(self):
        resolved, planned = self._settings()
        error = {"category": "oom", "stage": "decode", "message": "out of memory"}
        self.assertEqual(self._execute(resolved, planned, self._fake_subprocess(iterations=2, error=error)), 1)
        _, results, text = self._evidence()
        self.assertEqual(results["cases"][0]["status"], "oom")
        self.assertIn("partial", results["cases"][0]["flags"])
        self.assertIn("FAILURES, PARTIAL COVERAGE AND WARNINGS", text)

    def test_missing_ir_is_recorded_per_case_without_spawning(self):
        (self.model / "openvino_tokenizer.xml").unlink()
        resolved, planned = self._settings()
        spawned = mock.Mock()
        self.assertEqual(self._execute(resolved, planned, spawned), 1)
        spawned.assert_not_called()
        _, results, _ = self._evidence()
        self.assertEqual(results["cases"][0]["status"], "missing_ir")

    def test_ctrl_c_checkpoints_the_partial_case_and_exits_130(self):
        resolved, planned = self._settings()
        self.assertEqual(self._execute(resolved, planned, self._fake_subprocess(interrupt=True)), 130)
        _, results, _ = self._evidence()
        self.assertTrue(results["interrupted"])
        self.assertFalse(results["completed"])
        self.assertEqual(results["cases"][0]["error"]["category"], "interrupted")
        self.assertEqual(results["exit_code"], 130)

    def test_unconfirmed_cleanup_refuses_the_next_case_and_exits_one(self):
        resolved, planned = self._settings(context_tokens=[600, 700])

        def fake(spec, timeout_sec, **_kwargs):
            raise supervisor.ChildCleanupError({"case_id": spec["case_id"], "iterations": [],
                                                "probes": [], "cleanup_confirmed": False})

        self.assertEqual(self._execute(resolved, planned, fake), 1)
        _, results, _ = self._evidence()
        self.assertEqual([p["state"] for p in results["plan"]], ["done", "refused"])

    def test_accuracy_run_scores_after_the_child_and_reports_coverage(self):
        raw = settings.load_yaml(settings.DEFAULT_CONFIG_PATH)
        raw["model"].update(device="CPU", weight_format="fp16", model_dirs={"Qwen3.6-35B-A3B": str(self.model)})
        raw["profiles"] = [raw["profiles"][0], {"name": "plain_u8", "ov": {"KV_CACHE_PRECISION": "u8"},
                                                "scheduler": raw["profiles"][0]["scheduler"]}]
        raw["benchmark"]["output_dir"] = str(self.output)
        raw["accuracy"]["embedding_model"] = None
        resolved = settings._resolve_settings(raw, settings._parse_args(
            ["--accuracy", "--accuracy-tasks", "niah_single", "summary", "fact_sheet"]))
        planned = settings.plan_run(resolved)

        def fake(spec, timeout_sec, include_gpu=True, on_record=None, **_kwargs):
            self.assertEqual(len(spec["probes"]), 10)
            probes = []
            for probe in spec["probes"]:
                base = {"case_id": spec["case_id"], "iteration": 1 + probe["probe_index"], "warmup": False,
                        **{k: probe[k] for k in ("probe_index", "suite", "task", "depth", "sample", "fixture_id")}}
                if probe["probe_index"] == 3:
                    record = {**base, "status": "skipped", "skip_reason": "too small"}
                else:
                    answer = "the code is X" if spec["case_id"] == planned[0]["case_id"] else "nothing"
                    record = {**metrics.iteration_record(base["iteration"], 8000, 5, 0.3, 100.0), **base,
                              "status": "ok", "prompt_sha256": f"h{probe['probe_index']}",
                              "prediction_text": answer, "peak_ram_gb": 1.0, "memory_window": {"samples": 1}}
                on_record("probe", record)
                probes.append(record)
            return {"case_id": spec["case_id"], "load_ok": True, "error": None, "iterations": [],
                    "probes": probes, "cleanup_confirmed": True, "memory_baseline": dict(FAKE_MEM),
                    "peak_ram_pct": 20.0, "peak_ram_gb": 3.0, "device": {}}

        self.assertEqual(self._execute(resolved, planned, fake), 0)
        _, results, text = self._evidence()
        # A skipped probe is accounted for, as in the reference: the cases stay `ok` and the
        # rates are over the scored probes.
        self.assertEqual([c["status"] for c in results["cases"]], ["ok", "ok"])
        retrieval = results["cases"][1]["accuracy"]["retrieval"]
        self.assertEqual((retrieval["planned"], retrieval["completed"], retrieval["skipped"]), (6, 5, 1))
        self.assertEqual(retrieval["deltas"]["recall_rate_delta"], 0.0)
        generation = results["cases"][1]["accuracy"]["generation"]
        self.assertEqual(generation["identical_rate"], 0.0)
        self.assertIsNone(generation["similarity"])
        self.assertIsNone(results["cases"][0]["output_consistent"])  # probes are not repeats
        self.assertIn("Best retrieval: paged_min -- recall 0%, EM 0%, F1 0% over 5 probes", text)
        self.assertIn("SKIPPED: too small", text)
        self.assertIn("CASES (2 planned, run row by row)", text)
        self.assertIn("  8,000 tokens  accuracy | c01        c02", text)
        self.assertNotIn("the code is X", json.dumps(results))

    def test_cli_exit_codes_for_configuration_and_dependencies(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(benchmark.main(["--config", str(self.root / "missing.yaml")]), 2)
            with self.assertRaises(SystemExit) as caught:
                benchmark.main(["--contexts", "eight"])
            self.assertEqual(caught.exception.code, 2)
            with mock.patch.object(benchmark.importlib.util, "find_spec", return_value=None):
                self.assertEqual(benchmark.main(["--config", str(settings.DEFAULT_CONFIG_PATH)]), 2)


class TestPlanningIsSideEffectFree(unittest.TestCase):
    FIXTURE = settings.DEFAULT_CONFIG_PATH

    def test_list_profiles_prints_the_plan_without_side_effects(self):
        forbidden = mock.Mock(side_effect=AssertionError("planning must not do this"))
        for flags in ((), ("--accuracy",)):
            with self.subTest(flags=flags):
                out = io.StringIO()
                with mock.patch("sys.stdout", out), \
                        mock.patch.object(supervisor.multiprocessing, "get_context", forbidden), \
                        mock.patch("socket.socket.connect", forbidden), \
                        mock.patch.object(evidence, "create_run_dir", forbidden), \
                        mock.patch.object(evidence, "atomic_write_text", forbidden):
                    code = benchmark.main(["--config", str(self.FIXTURE), "--list-profiles", *flags])
                text = out.getvalue()
                self.assertEqual(code, 0)
                self.assertIn("  shared by every profile:", text)
                self.assertEqual(text.count("KV_CACHE_PRECISION=f16"), 1)  # shared settings once
                self.assertNotIn(str(SC_ROOT), text)  # redacted, repository-relative locations

    def test_planning_creates_no_files_and_loads_no_runtime(self):
        before = {p for p in (SC_ROOT / "monitoring").rglob("*")} if (SC_ROOT / "monitoring").exists() else set()
        code = ("import sys; from components.context_bench import benchmark; "
                f"rc = benchmark.main(['--config', r'{self.FIXTURE}', '--list-profiles', '--accuracy']); "
                "print(sorted(m for m in ('openvino','openvino_genai','transformers','numpy') if m in sys.modules)); "
                "sys.exit(rc)")
        completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=SC_ROOT, capture_output=True,
                                   text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip().splitlines()[-1], "[]")
        after = {p for p in (SC_ROOT / "monitoring").rglob("*")} if (SC_ROOT / "monitoring").exists() else set()
        self.assertEqual(before, after)


class TestMessageSanitizing(unittest.TestCase):
    def test_a_home_directory_with_a_space_is_fully_redacted(self):
        home = r"C:\Users\John Smith"
        with mock.patch.object(trial_runner.os.path, "expanduser", return_value=home):
            text = trial_runner.sanitize_message(f"cannot open {home}" + r"\models\x.xml")
        self.assertNotIn("Smith", text)
        self.assertIn("<home>", text)


if __name__ == "__main__":
    unittest.main()

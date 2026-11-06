# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Scripts/processing_stats_report.py: sources, timeline lanes and report tables."""

import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "Scripts" / "processing_stats_report.py"


def _load():
    # By path: a `scripts` namespace package from pywin32 can shadow this directory.
    spec = importlib.util.spec_from_file_location("_processing_stats_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rep = _load()

SID = "20261017-235509-d65d"
STAGES = [
    {"session_id": SID, "stage": "transcribe", "status": "done", "duration_sec": 100.0,
     "started_at": "2026-10-17T15:55:00.000+00:00", "ended_at": "2026-10-17T15:56:40.000+00:00"},
    {"session_id": SID, "stage": "summarize", "status": "failed", "duration_sec": 0.0,
     "started_at": "2026-10-17T15:56:41.000+00:00", "ended_at": "2026-10-17T15:56:41.000+00:00"},
]


def _call(capability, stage, submitted, latency_ms, status="ok", **extra):
    return {"session_id": SID, "capability": capability, "stage": stage, "status": status,
            "model": "m", "submitted_at": submitted, "queue_wait_ms": 500.0,
            "latency_ms": latency_ms, **extra}


CALLS = [
    _call("asr", "transcribe", "2026-10-17T15:55:00.000+00:00", 40_000.0),
    # Board OCR has no stage record: its lane is derived from the calls.
    _call("ocr", "board_ocr", "2026-10-17T15:55:10.000+00:00", 1_000.0),
    _call("ocr", "board_ocr", "2026-10-17T15:55:20.000+00:00", 2_000.0),
    _call("text_gen", "summarize", "2026-10-17T15:56:41.000+00:00", None, status="rejected"),
    _call("text_gen", "mindmap", "2026-10-17T15:56:45.000+00:00", 10_000.0,
          draft_tokens=100, accepted_tokens=75, ttft_ms=900.0),
]


def _session_dir(tmp_path):
    logs = tmp_path / SID / "logs"
    logs.mkdir(parents=True)
    (logs / "stage_events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in STAGES) + "\n{partial", encoding="utf-8")
    (logs / "model_calls.jsonl").write_text(
        "\n".join(json.dumps(r) for r in CALLS), encoding="utf-8")
    return tmp_path / SID


def test_lanes_derive_untracked_stages_and_skip_rejected_calls():
    lanes = {(label, kind): blocks for label, kind, blocks in rep.lanes(STAGES, CALLS)}
    (start, end, status), = lanes[("board_ocr", "derived")]
    assert end - start == pytest.approx(10.0 + 2.0)  # first call admitted -> last call ended
    assert status == "ok"
    assert len(lanes[("text_gen calls", "calls")]) == 1  # the rejected call never ran
    assert ("mindmap", "derived") in lanes and ("summarize", "stage") in lanes


def test_session_directory_report(tmp_path):
    text = rep.report([str(_session_dir(tmp_path))])
    assert "```mermaid\n---\ndisplayMode: compact\n---\ngantt" in text
    assert "    section board_ocr *" in text
    # A stage that failed at once still gets a 1 ms bar, marked critical.
    assert "summarize 0.0s :crit, 1792252601000, 1792252601001" in text
    assert re.search(r"^board_ocr \* +\|.*░", text, re.M)  # plain-text lane
    assert "| board_ocr * | from calls |" in text
    assert "75 / 100 (75%)" in text
    assert "| summarize | 0 ⚠ |" not in text and "| summarize | 0 |" in text


def test_api_json_source_requires_raw_records(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"raw": {"stages": STAGES, "model_calls": CALLS}}),
                    encoding="utf-8")
    assert "## Session `20261017-235509-d65d`" in rep.report([str(path)])
    path.write_text(json.dumps({"stages": []}), encoding="utf-8")
    with pytest.raises(SystemExit, match="raw=true"):
        rep.report([str(path)])


def test_powershell_export_with_bom_is_read(tmp_path):
    path = tmp_path / "stats.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"raw": {"stages": STAGES, "model_calls": CALLS}}).encode("utf-8"))
    assert "## Session `20261017-235509-d65d`" in rep.report([str(path)])


def test_stage_rows_count_each_attempts_own_calls_including_failures():
    stages = [
        {"session_id": SID, "stage": "summarize", "status": "failed", "duration_sec": 70.0,
         "started_at": "2026-10-17T15:00:00.000+00:00"},
        {"session_id": SID, "stage": "summarize", "status": "done", "duration_sec": 20.0,
         "started_at": "2026-10-17T15:02:00.000+00:00"},
    ]
    calls = [
        # The first attempt's only call ran 60 s and then failed.
        _call("text_gen", "summarize", "2026-10-17T15:00:01.000+00:00", 60_000.0,
              status="failed"),
        _call("text_gen", "summarize", "2026-10-17T15:02:01.000+00:00", 15_000.0),
    ]
    text = rep.session_section(SID, stages, calls)
    assert "| summarize | failed | 0.0 | 70.0 | 70.0 | 1 | 60.0 | 0.5 |" in text
    assert "| summarize | done | 120.0 | 140.0 | 20.0 | 1 | 15.0 | 0.5 |" in text


def test_acceptance_needs_both_counters():
    calls = [_call("text_gen", "summarize", "2026-10-17T15:56:45.000+00:00", 1_000.0,
                   draft_tokens=100)]
    row = next(line for line in rep.text_gen_table(calls).splitlines() if "| summarize" in line)
    assert "| — |" in row
    # 75/100 plus a call without an accepted count is 75/100 over 1 of 2 calls, not 75/200.
    calls.append(_call("text_gen", "summarize", "2026-10-17T15:57:45.000+00:00", 1_000.0,
                       draft_tokens=100, accepted_tokens=75))
    row = next(line for line in rep.text_gen_table(calls).splitlines() if "| summarize" in line)
    assert "75 / 100 (75%), 1 of 2 calls" in row


def _tg(stage, submitted, latency_ms, **extra):
    defaults = {"precision": "int4", "device": "GPU", "decoding": "plain", "ttft_ms": 2_000.0,
                "tpot_ms": 50.0, "decode_tps": 20.0, "input_tokens": 1_000, "output_tokens": 401}
    return _call("text_gen", stage, submitted, latency_ms, **{**defaults, **extra})


def test_text_gen_table_has_ttft_tpot_and_rates_per_stage_and_overall():
    calls = [_tg("summarize", "2026-10-17T15:56:45.000+00:00", 22_000.0),
             _tg("mindmap", "2026-10-17T15:57:10.000+00:00", 10_000.0, ttft_ms=1_000.0,
                 tpot_ms=25.0, decode_tps=40.0, output_tokens=361),
             _call("asr", "transcribe", "2026-10-17T15:55:00.000+00:00", 1_000.0)]
    lines = rep.text_gen_table(calls).splitlines()
    summarize = next(line for line in lines if line.startswith("| summarize"))
    # 401 tokens in 22 s is 18.2 tok/s overall; 1,000 prompt tokens in 2 s of TTFT is 500.
    assert "| 2,000 / 2,000 | 50.0 / 50.0 | 20.0 | 18.2 | 500 | 1,000 / 401 |" in summarize
    overall = next(line for line in lines if line.startswith("| **all stages**"))
    assert "| m | int4 | GPU | plain | 2 ⚠ | 1,000 / 2,000 | 25.0 / 50.0 |" in overall
    assert "| 2,000 / 762 |" in overall
    assert not any("asr" in line for line in lines)  # text generation only
    # A model seen in one stage needs no "all stages" row.
    assert "all stages" not in rep.text_gen_table(calls[:1])
    assert rep.text_gen_table(calls[2:]) == "No text-generation calls recorded.\n"


def test_session_lists_each_text_gen_call_with_its_timing():
    stages = [{"session_id": SID, "stage": "summarize", "status": "done", "duration_sec": 30.0,
               "started_at": "2026-10-17T15:56:40.000+00:00"}]
    calls = [_tg("summarize", "2026-10-17T15:56:45.000+00:00", 22_000.0),
             _tg("summarize", "2026-10-17T15:57:08.000+00:00", None, status="rejected",
                 ttft_ms=None, tpot_ms=None, decode_tps=None, input_tokens=None,
                 output_tokens=None)]
    text = rep.session_section(SID, stages, calls)
    section = text.split("### Text generation")[1]
    assert "Model: `m int4 GPU plain`." in section
    assert ("| summarize | 5.0 | ok | 1,000 / 401 | 500 | 22.0 | 2,000 | 50.0 | 20.0 | "
            "18.2 | 500 | — |") in section
    # Unavailable values are dashes, never zero.
    assert "| summarize | 28.0 | rejected | — / — | 500 | — | — | — | — | — | — | — |" in section
    # A second model in the session gets a column instead of the one-line note.
    calls.append(_tg("mindmap", "2026-10-17T15:57:30.000+00:00", 5_000.0, decoding="dflash",
                     draft_tokens=200, accepted_tokens=150))
    section = rep.session_section(SID, stages, calls).split("### Text generation")[1]
    assert "Model:" not in section and "| Model |" in section
    assert "| m int4 GPU dflash |" in section and "| 150 / 200 (75%) |" in section


def test_a_renamed_session_directory_keeps_its_session_id(tmp_path):
    import sqlite3
    session = _va_session(tmp_path)
    renamed = session.rename(tmp_path / f"{SID}-qwen3")
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.execute("CREATE TABLE sessions (session_id TEXT, state TEXT, stages TEXT, error TEXT)")
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                 (SID, "completed", json.dumps({"transcribe": "done"}), None))
    conn.commit()
    conn.close()
    text = rep.report([str(renamed)])
    # One session: the VA runs and the session row join the records' own id.
    assert "1 session(s)" in text and f"## Session `{SID}-qwen3`" not in text
    assert "Session state: **completed**." in text
    assert "    section va front" in text.split(f"## Session `{SID}`")[1]


def test_cli_writes_the_report(tmp_path):
    out = tmp_path / "report.md"
    assert rep.main([str(_session_dir(tmp_path)), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Processing-Time Report")


# --- video analytics -----------------------------------------------------------
VA_EPOCH = 1792252500  # 2026-10-17T15:55:00Z, the launch time in a runner log's name


def _stamp(offset_sec):
    """A runner log timestamp: local wall time, as the runner's logging writes it."""
    from datetime import datetime
    return datetime.fromtimestamp(VA_EPOCH + offset_sec).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _runner_log(pipeline, *events, tail="", source="C:/v/in.mp4"):
    lines = [f"{_stamp(0.5)} [INFO] utils.gstreamer_env: DL Streamer",
             f"{_stamp(0.6)} [INFO] va.runner.{pipeline}: Pipeline description: "
             f"{'rtspsrc' if source.startswith('rtsp://') else 'filesrc'} location={source} ! "
             "gvadetect model=C:/m/yolov8m-pose.xml device=NPU threshold=0.5 ! gvaclassify "
             "model=C:/m/resnet18.xml device=GPU ! fakesink"]
    lines += [f"{_stamp(offset)} [{level}] va.runner.{pipeline}: {message}"
              for offset, level, message in events]
    return "\n".join(lines) + "\n" + tail


def _va_session(tmp_path):
    session = _session_dir(tmp_path)
    logs = session / "raw" / "va" / "logs"
    logs.mkdir(parents=True)
    fps = "FpsCounter(overall 60.00sec): total=15.00 fps, number-streams=1, per-stream=15.00 fps\n"
    (logs / f"front_{VA_EPOCH}.log").write_text(_runner_log(
        "front", (2.0, "INFO", "PLAYING"), (62.0, "INFO", "EOS"),
        (62.1, "INFO", "Teardown: set_state(NULL) -> success"), tail=fps), encoding="utf-8")
    # The app was stopped under it: Ctrl+C reached the runner mid-pipeline.
    (logs / f"back_{VA_EPOCH + 1}.log").write_text(_runner_log(
        "back", (3.0, "INFO", "PLAYING"), (30.0, "INFO", "Teardown: set_state(NULL) -> success"),
        tail="Traceback (most recent call last):\nKeyboardInterrupt\n" + fps.replace("60.00", "26.00")),
        encoding="utf-8")
    (logs / f"content_{VA_EPOCH + 2}.log").write_text(_runner_log(
        "content", (4.0, "INFO", "PLAYING"), (9.0, "ERROR", "ERROR from filesrc: Not found | x")),
        encoding="utf-8")
    (session / "raw" / "va" / "front_posture.txt").write_text("{}\n" * 900, encoding="utf-8")
    return session


def test_va_runner_logs_become_lanes_and_a_table(tmp_path):
    session = _va_session(tmp_path)
    runs = {r["pipeline"]: r for r in rep.load_va(session)}
    front, back, content = runs["front"], runs["back"], runs["content"]
    assert front["status"] == "done" and front["fps"] == 15.0
    assert front["start"] == pytest.approx(VA_EPOCH + 2.0)  # PLAYING, in true epoch time
    assert front["end"] - front["start"] == pytest.approx(60.1)
    assert front["models"] == ["yolov8m-pose (NPU)", "resnet18 (GPU)"]
    assert front["outputs"] == {"front_posture": 900}
    assert back["status"] == "interrupted" and "KeyboardInterrupt" in back["error"]
    assert content["status"] == "failed" and content["error"].startswith("ERROR from filesrc")

    text = rep.report([str(session)])
    assert "3 VA pipeline run(s)" in text
    assert "    section va front" in text and "va back 27.0s :crit," in text
    assert re.search(r"^va front +\|.*█", text, re.M)
    assert re.search(r"^va back +\|.*X", text, re.M)
    # The fixture's input file does not exist, so its length is unknown.
    assert "| front | `front_1792252500.log` | in.mp4 | done | 2.0 | 62.1 | 60.1 | — | — | — | " \
           "15.0 | front_posture 900 | yolov8m-pose (NPU), resnet18 (GPU) |" in text
    # The fixture's session ended without a `va` stage record.
    assert "No `va` stage record" in text
    assert "| va front |" not in text.split("### Video analytics")[0]  # not a stage row


def test_a_stopped_pipeline_is_a_normal_end(tmp_path):
    log = tmp_path / f"front_{VA_EPOCH}.log"
    log.write_text(_runner_log("front", (2.0, "INFO", "PLAYING"),
                               (40.0, "INFO", "Stop requested; sending EOS"),
                               (40.5, "INFO", "EOS")), encoding="utf-8")
    assert rep.va_run(log, SID)["status"] == "stopped"
    assert rep.va_run(tmp_path / "front_notes.log", SID) is None  # not a runner log


def test_session_store_row_shows_stages_that_never_started(tmp_path):
    import sqlite3
    session = _session_dir(tmp_path)
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.execute("CREATE TABLE sessions (session_id TEXT, state TEXT, stages TEXT, error TEXT)")
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (
        SID, "failed", json.dumps({"transcribe": "done", "va": "running",
                                   "report": "pending"}), "process interrupted (restart)"))
    conn.commit()
    conn.close()
    text = rep.report([str(session)])
    assert "Session state: **failed** (process interrupted (restart))." in text
    assert "Declared stages: transcribe done, va running, report pending." in text
    assert "Never started: report" in text


def _mp4(path, seconds, version=0):
    """The boxes mp4_duration reads: ftyp, then moov/mvhd with a 1 kHz timescale."""
    import struct
    if version == 0:
        body = struct.pack(">B3xIIII", 0, 0, 0, 1000, int(seconds * 1000))
    else:
        body = struct.pack(">B3xQQIQ", 1, 0, 0, 1000, int(seconds * 1000))
    mvhd = struct.pack(">I4s", 8 + len(body) + 80, b"mvhd") + body + bytes(80)
    path.write_bytes(struct.pack(">I4s4sI", 16, b"ftyp", b"isom", 0)
                     + struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd)
    return path


def test_mp4_duration_reads_the_movie_header(tmp_path):
    assert rep.mp4_duration(_mp4(tmp_path / "a.mp4", 300.134)) == pytest.approx(300.134)
    assert rep.mp4_duration(_mp4(tmp_path / "b.mp4", 42.5, version=1)) == pytest.approx(42.5)
    (tmp_path / "c.mp4").write_bytes(b"not a movie")
    assert rep.mp4_duration(tmp_path / "c.mp4") is None
    assert rep.mp4_duration(tmp_path / "missing.mp4") is None


def test_va_duration_is_read_against_the_input_length(tmp_path):
    movie = _mp4(tmp_path / "front.mp4", 120.0)
    log = tmp_path / f"front_{VA_EPOCH}.log"
    log.write_text(_runner_log("front", (2.0, "INFO", "PLAYING"), (62.0, "INFO", "EOS"),
                               source=movie.as_posix()), encoding="utf-8")
    run = rep.va_run(log, SID)
    assert run["source"] == "front.mp4" and run["input_sec"] == pytest.approx(120.0)
    row = next(line for line in rep.va_table([run], [], VA_EPOCH).splitlines()
               if line.startswith("| front"))
    # 120 s of input processed in 60 s: the whole file, at twice real time.
    assert "| front.mp4 | done |" in row and "| 60.0 | 120.0 | — | 2.0x |" in row
    # A run that did not reach EOS did not cover its input, so it has no speed.
    assert rep._speed({**run, "status": "interrupted"}) == "—"
    rtsp = tmp_path / f"back_{VA_EPOCH}.log"
    rtsp.write_text(_runner_log("back", (2.0, "INFO", "PLAYING"),
                                source="rtsp://user:secret@cam/1"), encoding="utf-8")
    assert rep.va_run(rtsp, SID)["source"] == "RTSP stream (live)"  # no credentials


def test_a_session_in_progress_is_reported_as_a_snapshot(tmp_path):
    """A report made mid-session must not read as a session that ended early."""
    import sqlite3
    session = _session_dir(tmp_path)
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.execute("CREATE TABLE sessions (session_id TEXT, state TEXT, stages TEXT, error TEXT)")
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (SID, "running", json.dumps(
        {"transcribe": "done", "va": "running", "segmentation": "pending"}), None))
    conn.commit()
    conn.close()
    logs = session / "raw" / "va" / "logs"
    logs.mkdir(parents=True)
    (logs / f"front_{VA_EPOCH}.log").write_text(
        _runner_log("front", (2.0, "INFO", "PLAYING")), encoding="utf-8")  # still written
    text = rep.report([str(session)])
    assert "> **In progress.** Snapshot taken" in text
    assert "Not started yet: segmentation (waits for va)." in text
    assert "Never started" not in text
    assert "Workflow window so far:" in text
    assert "| running (time so far) |" in text
    assert "The `va` stage is still open" in text and "No `va` stage record" not in text
    assert ":active, " in text.split("section va front")[1].splitlines()[1]
    # Time so far runs to the report, not to the runner's buffered last line.
    run = rep.load_va(session)[0]
    assert run["end"] >= datetime.now().timestamp() - 5


def _frames(path, seconds, partial=False):
    """gvametapublish json-lines: one frame per line, ``timestamp`` in ns, last key."""
    lines = [json.dumps({"objects": [], "timestamp": int(t * 1e9)}) for t in seconds]
    path.write_text("\n".join(lines) + "\n" + ('{"objects":[{"x":1' if partial else ""),
                    encoding="utf-8")
    return path


def test_media_position_is_the_last_published_frame(tmp_path):
    assert rep.media_position(_frames(tmp_path / "a.txt", [0, 1.5, 261.0])) == 261.0
    # A line still being written has no timestamp yet.
    assert rep.media_position(_frames(tmp_path / "b.txt", [0, 9.25], partial=True)) == 9.25
    (tmp_path / "c.txt").write_text("", encoding="utf-8")
    assert rep.media_position(tmp_path / "c.txt") is None
    assert rep.media_position(tmp_path / "missing.txt") is None


def test_a_run_that_ended_early_says_how_much_input_it_processed(tmp_path):
    """The VA duration of an interrupted run is short of the input length; the
    report has to say that the input was not finished, not leave it to guess."""
    session = _va_session(tmp_path)
    va_dir = session / "raw" / "va"
    _frames(va_dir / "back_posture.txt", [i / 15 for i in range(int(26.0 * 15))])
    movie = _mp4(tmp_path / "in.mp4", 300.1).as_posix()
    log = va_dir / "logs" / f"back_{VA_EPOCH + 1}.log"
    log.write_text(log.read_text(encoding="utf-8").replace("C:/v/in.mp4", movie),
                   encoding="utf-8")
    back = next(r for r in rep.load_va(session) if r["pipeline"] == "back")
    assert back["processed_sec"] == pytest.approx(25.933, abs=1e-3)
    text = rep.report([str(session)])
    row = next(line for line in text.splitlines() if line.startswith("| back |"))
    assert "| 300.1 | 25.9 (9%) | 1.0x |" in row
    # front has no position (its fixture results carry no timestamps): not listed.
    assert "**Input not fully processed** (back 25.9 of 300.1 s, interrupted): " in text


def test_processed_input_belongs_to_the_latest_attempt(tmp_path):
    session = _va_session(tmp_path)
    logs = session / "raw" / "va" / "logs"
    (logs / f"front_{VA_EPOCH + 100}.log").write_text(_runner_log(
        "front", (101.0, "INFO", "PLAYING"), (130.0, "INFO", "EOS")), encoding="utf-8")
    _frames(session / "raw" / "va" / "front_posture.txt", [0, 28.0])
    attempts = [r for r in rep.load_va(session) if r["pipeline"] == "front"]
    assert [r["processed_sec"] for r in attempts] == [None, 28.0]


def test_a_running_session_row_after_the_app_stopped_is_not_in_progress(tmp_path):
    """The store keeps a session `running` until the next start; a report made
    after Ctrl+C must not present the dead session as a live snapshot."""
    import sqlite3
    session = _va_session(tmp_path)  # back: interrupted by Ctrl+C
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.execute("CREATE TABLE sessions (session_id TEXT, state TEXT, stages TEXT, error TEXT)")
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (SID, "running", json.dumps(
        {"transcribe": "done", "va": "running", "segmentation": "pending"}), None))
    conn.commit()
    conn.close()
    text = rep.report([str(session)])
    assert "In progress" not in text and "Workflow window:" in text
    assert "The app stopped while this session ran (VA runners were interrupted)" in text
    assert "Never started: segmentation" in text
    # A stage closed at shutdown is the same evidence, VA or not.
    assert rep._stopped_evidence([{"error_class": "Shutdown"}], []) is not None
    assert rep._stopped_evidence(STAGES, []) is None

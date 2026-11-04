#!/usr/bin/env python
# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Turn processing-time statistics into a Markdown report with a timing diagram.

    python Scripts/processing_stats_report.py storage/smart-classroom/<session>
    python Scripts/processing_stats_report.py stats.json -o report.md
    python Scripts/processing_stats_report.py <session dir> <session dir> ...

A source is a session directory (its ``logs/stage_events.jsonl`` and
``logs/model_calls.jsonl``, which survive restarts) or the JSON saved from
``GET /processing-stats?raw=true``. Each session gets a timeline: one lane per
workflow stage plus one per model capability, drawn as a Mermaid Gantt chart
and as plain text. Each VLM/LLM call of a session gets a row with its TTFT,
TPOT and tokens/s; the closing tables give the same per stage and per model
across sessions. Distributions reuse ``utils.processing_stats``, so they match
the API. See smart-classroom/docs/dev-guide/processing-time-statistics/processing-time-statistics.md.

Video analytics runs as three GStreamer subprocesses (front, back, content)
that make no model calls through the app, and its ``va`` stage record covers
all three together. A session directory therefore also contributes one lane
per VA pipeline attempt, read from the runner logs in ``raw/va/logs``: each
runner writes its own log, so it is there even when the app was stopped before
VA finished and no ``va`` stage record exists.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from itertools import groupby
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from utils.processing_stats import call_groups, call_rates, stage_groups  # noqa: E402  (needs _ROOT)

_OK_STAGE, _OK_CALL = "done", "ok"
_WIDTH = 64  # columns of the plain-text timeline


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a crash mid-append can leave a partial last line
    return rows


def load(source: Path) -> tuple:
    """Return ``(stages, calls)`` records from a session directory or API JSON."""
    if not source.exists():
        raise SystemExit(f"{source}: not found (run from the app root, smart-classroom/Windows)")
    if source.is_dir():
        logs = source / "logs" if (source / "logs").is_dir() else source
        return _jsonl(logs / "stage_events.jsonl"), _jsonl(logs / "model_calls.jsonl")
    # utf-8-sig: Windows PowerShell 5.1 "Set-Content -Encoding utf8" writes a BOM.
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    raw = data.get("raw")
    if raw is None:
        raise SystemExit(f"{source}: no raw records; save GET /processing-stats?raw=true")
    return raw.get("stages", []), raw.get("model_calls", [])


def _session_dir(source: Path):
    """The session directory a source names, or None for an API JSON file."""
    if not source.is_dir():
        return None
    return source.parent if source.name == "logs" else source


def _session_id(session_dir: Path, records: list) -> str:
    """The session id its records carry, else the directory name.

    A session directory copied or renamed for safekeeping (``<id>-qwen3``) keeps
    its records' id, and the VA runs and session row must join that id.
    """
    ids = {r.get("session_id") for r in records if r.get("session_id")}
    return ids.pop() if len(ids) == 1 else session_dir.name


# ---------------------------------------------------------------------------
# Video analytics runner logs
# ---------------------------------------------------------------------------
_VA_PIPELINES = ("front", "back", "content")
_VA_LOG = re.compile(r"^(front|back|content)_(\d+)\.log$")
_LOG_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) \[(\w+)\] \S+: (.*)$", re.M)
_FPS = re.compile(r"FpsCounter\((overall|average) ([\d.]+)sec\): total=([\d.]+) fps")
_VA_MODEL = re.compile(r"\bgva(?:detect|classify)\b[^!]*?\bmodel=(\S+)[^!]*?\bdevice=(\S+)")
_VA_SOURCE = re.compile(r"\b(filesrc|rtspsrc) location=(\S+)")
# A runner log untouched this long, without a teardown, belongs to a stopped app.
_VA_LIVE_SEC = 30
_OK_VA = ("done", "stopped", "running")


def mp4_duration(path: Path):
    """Media duration in seconds from an MP4/MOV ``moov/mvhd`` box; None if unreadable.

    Reads box headers only (the sample data is skipped), so it costs a few small
    reads even for a large recording, and needs no media tooling.
    """
    import struct

    def boxes(f, end):
        while f.tell() + 8 <= end:
            start = f.tell()
            size, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if size == 1:
                size, header = struct.unpack(">Q", f.read(8))[0], 16
            elif size == 0:
                size = end - start
            if size < header:
                return
            yield kind, start + header, start + size
            f.seek(start + size)

    try:
        with open(path, "rb") as f:
            end = f.seek(0, 2)
            f.seek(0)
            for kind, body, box_end in boxes(f, end):
                if kind != b"moov":
                    continue
                f.seek(body)
                for inner, inner_body, _ in boxes(f, box_end):
                    if inner != b"mvhd":
                        continue
                    f.seek(inner_body)
                    version = f.read(1)[0]
                    f.seek(3, 1)  # flags
                    if version == 1:
                        _, _, timescale, duration = struct.unpack(">QQIQ", f.read(28))
                    else:
                        _, _, timescale, duration = struct.unpack(">IIII", f.read(16))
                    return duration / timescale if timescale else None
    except (OSError, struct.error, IndexError):
        return None
    return None


def _count_lines(path: Path) -> int:
    with open(path, "rb") as f:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))


_FRAME_TS = re.compile(rb'"timestamp":\s*(\d+)')
_TAIL_BYTES = 256 << 10  # many result lines: a crowded frame's line is a few KB


def media_position(path: Path):
    """Media time (s) of the last frame in a gvametapublish json-lines file; None if none.

    Every line is one frame and ends with its ``timestamp``, the frame's
    position in the input in nanoseconds. Only the file's tail is read; a
    partial last line (still being written) has no timestamp yet and is skipped.
    """
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - _TAIL_BYTES))
            stamps = _FRAME_TS.findall(f.read())
    except OSError:
        return None
    return max(int(s) for s in stamps) / 1e9 if stamps else None


def va_run(path: Path, session_id: str):
    """One VA pipeline attempt from its runner log, or None if the name is not one.

    The log timestamps are local wall time; the file name carries the launch
    time as a Unix epoch, which pins the zone of the machine that wrote them.
    Status: ``done`` (EOS), ``stopped`` (stop requested, EOS pushed),
    ``failed`` (an ERROR was logged), ``running`` (still being written) or
    ``interrupted`` (the runner never got to tear down normally: the app was
    stopped or killed under it).
    """
    match = _VA_LOG.match(path.name)
    if not match:
        return None
    pipeline, epoch = match.group(1), float(match.group(2))
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = _LOG_LINE.findall(text)
    run = {"session_id": session_id, "pipeline": pipeline, "log": path.name,
           "start": epoch, "end": epoch, "status": "failed", "fps": None,
           "error": None, "models": [], "source": None, "input_sec": None,
           "processed_sec": None}
    if not lines:
        run["error"] = "the runner logged nothing"
        return run

    def local(stamp):
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f").timestamp()

    # Whole quarter hours between the launch epoch and the first local stamp.
    shift = round((epoch - local(lines[0][0])) / 900.0) * 900.0
    playing = next((stamp for stamp, _, message in lines if message == "PLAYING"), None)
    run["start"] = local(playing or lines[0][0]) + shift
    run["end"] = local(lines[-1][0]) + shift
    fps = _FPS.findall(text)
    if fps:
        overall = [f for f in fps if f[0] == "overall"]
        _, elapsed, total = (overall or fps)[-1]
        run["fps"] = float(total)
        # A runner killed outright never logs a final timestamped line.
        run["end"] = max(run["end"], run["start"] + float(elapsed))
    messages = [message for _, _, message in lines]
    errors = [message for _, level, message in lines if level == "ERROR"]
    if errors:
        run["status"], run["error"] = "failed", errors[0][:200]
    elif any(m.startswith("Stop requested") for m in messages):
        run["status"] = "stopped"
    elif "EOS" in messages:
        run["status"] = "done"
    elif ("Traceback" not in text and not any(m.startswith("Teardown") for m in messages)
          and datetime.now().timestamp() - path.stat().st_mtime < _VA_LIVE_SEC):
        run["status"] = "running"
        # Time so far runs to now: the runner's stdout (the FPS lines) is
        # block-buffered into the log and trails the pipeline by seconds.
        run["end"] = max(run["end"], datetime.now().timestamp())
    else:
        run["status"] = "interrupted"
        if "KeyboardInterrupt" in text:
            run["error"] = "KeyboardInterrupt (Ctrl+C reached the runner)"
    description = next((m for m in messages if m.startswith("Pipeline description:")), "")
    for model, device in _VA_MODEL.findall(description):
        label = f"{Path(model).stem} ({device})"
        if label not in run["models"]:
            run["models"].append(label)
    source = _VA_SOURCE.search(description)
    if source and source.group(1) == "rtspsrc":
        run["source"] = "RTSP stream (live)"  # the URL may carry credentials
    elif source:
        # The input's own length, for reading the processing time against it.
        run["source"] = Path(source.group(2)).name
        run["input_sec"] = mp4_duration(Path(source.group(2)))
    return run


def load_va(source: Path, session_id: str = None) -> list:
    """Every VA pipeline attempt of a session directory, in launch order."""
    session_dir = _session_dir(source)
    log_dir = session_dir / "raw" / "va" / "logs" if session_dir else None
    if log_dir is None or not log_dir.is_dir():
        return []
    session_id = session_id or session_dir.name
    runs = [r for r in (va_run(p, session_id) for p in log_dir.iterdir()) if r]
    runs.sort(key=lambda r: r["start"])
    # Result records per pipeline (frames with published metadata, per file),
    # and how far into its input the pipeline got: the last frame published.
    va_dir = log_dir.parent
    for pipeline in _VA_PIPELINES:
        files = sorted(va_dir.glob(f"{pipeline}_*.txt"))
        outputs = {p.stem: _count_lines(p) for p in files}
        attempts = [r for r in runs if r["pipeline"] == pipeline]
        for r in attempts:
            r["outputs"] = outputs
        positions = [s for s in map(media_position, files) if s is not None]
        if attempts and positions:
            # The result files are the pipeline's, not per attempt: the latest wrote last.
            attempts[-1]["processed_sec"] = max(positions)
    return runs


def load_session_rows(source: Path, session_id: str = None) -> dict:
    """The session store's rows (state, declared stages) next to a session directory.

    Read-only and best effort: the store records which stages a session
    declared and how each one ended, including stages that never started.
    """
    session_dir = _session_dir(source)
    db = session_dir.parent / "sessions.db" if session_dir else None
    if db is None or not db.is_file():
        return {}
    session_id = session_id or session_dir.name
    import sqlite3
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            columns = {c[1] for c in conn.execute("PRAGMA table_info(sessions)")}
            request = "request" if "request" in columns else "NULL"
            rows = conn.execute(
                f"SELECT session_id, state, stages, error, {request} FROM sessions "
                "WHERE session_id = ?", (session_id,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out = {}
    for session_id, state, stages, error, request in rows:
        try:
            stages = json.loads(stages) if stages else {}
            request = json.loads(request) if request else {}
        except json.JSONDecodeError:
            stages, request = stages if isinstance(stages, dict) else {}, {}
        out[session_id] = {"state": state, "stages": stages, "error": error,
                           "video_sources": (request or {}).get("video_sources") or {}}
    return out


def requested_va_pipelines(row) -> dict:
    """``{pipeline: name the session used}`` for the VA sources a session requested."""
    from utils.va_completion import VA_SOURCE_ALIASES
    out = {}
    for name, source in ((row or {}).get("video_sources") or {}).items():
        if source:
            out.setdefault(VA_SOURCE_ALIASES.get(name, name), name)
    return out


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------
def _ts(text):
    try:
        return datetime.fromisoformat(text).timestamp() if text else None
    except ValueError:
        return None


def stage_interval(r):
    start = _ts(r.get("started_at"))
    if start is None:
        return None
    duration = r.get("duration_sec")
    end = start + duration if isinstance(duration, (int, float)) else _ts(r.get("ended_at"))
    return (start, end) if end is not None else None


def call_interval(r):
    """Admission to end of the call; rejected calls never ran."""
    submitted, latency = _ts(r.get("submitted_at")), r.get("latency_ms")
    if submitted is None or not isinstance(latency, (int, float)):
        return None
    start = submitted + (r.get("queue_wait_ms") or 0) / 1000.0
    return start, start + latency / 1000.0


def lanes(stages: list, calls: list, va_runs: list = ()) -> list:
    """``[(label, kind, [(start, end, status)])]`` in order of first activity.

    kind is ``stage``, ``derived`` (a stage seen only through its model calls,
    such as board OCR or an interrupted stage), ``va`` (one VA pipeline, one
    block per runner attempt) or ``calls``.
    """
    out = {}
    for r in stages:
        span = stage_interval(r)
        if span:
            out.setdefault((r.get("stage"), "stage"), []).append((*span, r.get("status")))
    for r in va_runs:
        out.setdefault((f"va {r['pipeline']}", "va"), []).append(
            (r["start"], r["end"], r["status"]))
    tracked = {stage for stage, kind in out if kind == "stage"}
    for r in calls:
        span = call_interval(r)
        if not span:
            continue
        out.setdefault((f"{r.get('capability')} calls", "calls"), []).append(
            (*span, r.get("status")))
        stage = r.get("stage")
        if stage not in tracked:
            out.setdefault((stage or "unattributed", "derived"), []).append(
                (*span, r.get("status")))
    result = []
    for (label, kind), blocks in out.items():
        if kind == "derived":
            # One span from the first to the last call of that stage.
            failed = any(s != _OK_CALL for _, _, s in blocks)
            blocks = [(min(b[0] for b in blocks), max(b[1] for b in blocks),
                       "failed" if failed else _OK_CALL)]
        result.append((label, kind, sorted(blocks)))
    order = {"stage": 0, "derived": 0, "va": 0, "calls": 1}
    return sorted(result, key=lambda lane: (order[lane[1]], lane[2][0][0]))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _ok(kind, status):
    if kind == "va":
        return status in _OK_VA
    return status == (_OK_CALL if kind in ("calls", "derived") else _OK_STAGE)


def _num(value, digits=1):
    return "—" if value is None else f"{value:,.{digits}f}"


def mermaid(session: str, rows: list) -> str:
    lines = ["```mermaid", "---", "displayMode: compact", "---", "gantt",
             f"    title Session {session}", "    dateFormat x", "    axisFormat %H:%M:%S"]
    for label, kind, blocks in rows:
        lines.append(f"    section {label}{' *' if kind == 'derived' else ''}")
        for start, end, status in blocks:
            tag = "" if _ok(kind, status) else "crit, "
            if (kind == "derived" and _ok(kind, status)) or status == "running":
                tag = "active, "
            name = f"{label.split()[0] if kind == 'calls' else label} {end - start:.1f}s"
            # At least 1 ms, so a stage that failed at once still gets a bar.
            first = int(start * 1000)
            lines.append(f"    {name} :{tag}{first}, {max(first + 1, int(end * 1000))}")
    lines.append("```")
    return "\n".join(lines)


def ascii_chart(rows: list, t0: float, t1: float, width: int = _WIDTH) -> str:
    span = max(t1 - t0, 1e-3)

    def col(t):
        return min(width - 1, max(0, int((t - t0) / span * width)))

    label_w = max(len(label) + 2 for label, _, _ in rows)
    lines = []
    for label, kind, blocks in rows:
        cells = [" "] * width
        for start, end, status in blocks:
            mark = "█" if _ok(kind, status) else "X"
            if kind == "derived" and mark == "█":
                mark = "░"
            for i in range(col(start), max(col(start), col(end)) + 1):
                cells[i] = mark
        name = f"{label} *" if kind == "derived" else label
        lines.append(f"{name:<{label_w}}|{''.join(cells)}|")
    axis = [" "] * (width + 1)
    marks = ["-"] * width
    for k in range(5):
        c = min(width - 1, round(k * (width - 1) / 4))
        marks[c] = "+"
        text = f"{k * span / 4:.0f}s"
        at = max(0, min(c - len(text) // 2, width + 1 - len(text)))
        axis[at:at + len(text)] = text
    lines.append(" " * label_w + "+" + "".join(marks) + "+")
    lines.append(" " * (label_w + 1) + "".join(axis).rstrip())
    return "```text\n" + "\n".join(lines) + "\n```"


def _calls_per_attempt(calls, stage, blocks):
    """Model calls of each attempt (block) of ``stage``.

    Attempts of one stage run one after another (a retry starts after the
    previous attempt ended), so a call belongs to the last attempt that started
    before it was submitted; a call without a usable time goes to the first.
    """
    per = [[] for _ in blocks]
    for r in calls:
        if r.get("stage") != stage:
            continue
        submitted = _ts(r.get("submitted_at"))
        index = 0
        if submitted is not None:
            # Both timestamps have millisecond resolution.
            started = [i for i, b in enumerate(blocks) if b[0] <= submitted + 0.001]
            index = started[-1] if started else 0
        per[index].append(r)
    return per


def _totals(rows):
    """Call count, model time and queue wait (s); model time includes failed and
    cancelled calls, whose inference still occupied the stage."""
    model = sum(r.get("latency_ms") or 0 for r in rows) / 1000.0
    queued = sum(r.get("queue_wait_ms") or 0 for r in rows) / 1000.0
    return len(rows), model, queued


def _waits_for(stage: str, stages: dict) -> list:
    """The declared stages ``stage`` runs after that have not finished yet."""
    try:
        from utils.pipeline_catalog import STAGE_RUN_AFTER
    except Exception:  # noqa: BLE001 - the report must render without the app config
        return []
    return [s for s in STAGE_RUN_AFTER.get(stage, ()) if stages.get(s) not in (None, "done", "skipped")]


def _stopped_evidence(stages: list, va_runs: list):
    """Why the app evidently stopped under a session, or None.

    The session store keeps a session ``running`` until the next start marks it
    failed, so a report made after the app was stopped would otherwise read as
    a snapshot of a session still in progress.
    """
    if any(r.get("error_class") == "Shutdown" for r in stages):
        return "stages were recorded as interrupted at shutdown"
    if any(r["status"] == "interrupted" for r in va_runs):
        return "VA runners were interrupted"
    return None


def _state_lines(row, live: bool = False, stopped: str = None) -> list:
    """The session store's verdict: final state and every declared stage's status."""
    if not row:
        return []
    stages = row.get("stages") or {}
    lines = [f"Session state: **{row.get('state') or '—'}**"
             + (f" ({row['error']})" if row.get("error") else "") + "."]
    if row.get("state") == "running" and not live and stopped:
        lines.append(f"The app stopped while this session ran ({stopped}); the session "
                     "store says running until the app's next start marks the session failed.")
    if stages:
        lines.append("Declared stages: " + ", ".join(f"{s} {st}" for s, st in stages.items()) + ".")
    pending = [s for s, st in stages.items() if st == "pending"]
    if pending and live:
        waits = [f"{s} (waits for {', '.join(w)})" if (w := _waits_for(s, stages)) else s
                 for s in pending]
        lines.append(f"Not started yet: {', '.join(waits)}.")
    elif pending:
        lines.append(f"Never started: {', '.join(pending)} - the session ended before their "
                     "turn, so they have no timing.")
    return lines


def _processed(r) -> str:
    """How far into its input the run got, and the share of the input that is."""
    done, total = r.get("processed_sec"), r.get("input_sec")
    if done is None:
        return "—"
    return f"{done:,.1f}" + (f" ({min(done / total, 1.0):.0%})" if total else "")


def _speed(r) -> str:
    """Seconds of input processed per second of processing.

    A pipeline whose stream output syncs to the clock plays a file in real
    time (about 1.0x); with the stream output off (headless sessions) nothing
    holds it back and a 5-minute file can finish in under a minute. Without
    result files to read the position from, only a run that reached EOS is
    known to have covered its whole input.
    """
    duration = r["end"] - r["start"]
    covered = r.get("processed_sec")
    if covered is None and r["status"] == "done":
        covered = r.get("input_sec")
    if not covered or duration <= 0:
        return "—"
    return f"{covered / duration:.1f}x"


def va_table(va_runs: list, stages: list, t0: float, live: bool = False,
             requested: dict = None) -> str:
    """One row per VA pipeline attempt, then how the ``va`` stage itself was recorded."""
    out = ["| Pipeline | Attempt | Input | Status | Start (s) | End (s) | Duration (s) | "
           "Input length (s) | Processed (s) | Speed | FPS | Output records | Models (device) |",
           "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    short = []
    for r in va_runs:
        status = r["status"] + (f": {r['error']}" if r.get("error") else "")
        if r["status"] == "running":
            status = "running (time so far)"
        outputs = ", ".join(f"{name} {count:,}" for name, count in (r.get("outputs") or {}).items())
        out.append(f"| {r['pipeline']} | `{r['log']}` | {r.get('source') or '—'} | {status} | "
                   f"{r['start'] - t0:,.1f} | {r['end'] - t0:,.1f} | {r['end'] - r['start']:,.1f} | "
                   f"{_num(r.get('input_sec'))} | {_processed(r)} | {_speed(r)} | "
                   f"{_num(r['fps'])} | {outputs or '—'} | {', '.join(r['models']) or '—'} |")
        done, total = r.get("processed_sec"), r.get("input_sec")
        # A second or more of input left: more than the last frame's own length.
        if (r["status"] not in ("done", "running") and done is not None and total
                and total - done >= 1.0):
            short.append(f"{r['pipeline']} {done:,.1f} of {total:,.1f} s, {r['status']}")
    records = [r for r in stages if r.get("stage") == "va"]
    out.append("")
    ran = {r["pipeline"] for r in va_runs}
    missing = [f"{p}" + (f" (requested as `{name}`)" if name != p else "")
               for p, name in (requested or {}).items() if p not in ran]
    if missing:
        out.append(f"**Requested but never launched** (no runner log): {', '.join(missing)}.")
    if records:
        for r in records:
            detail = f" ({r['error_detail']})" if r.get("error_detail") else ""
            out.append(f"The `va` stage covering all pipelines was recorded as "
                       f"**{r.get('status')}** after {_num(r.get('duration_sec'))} s{detail}.")
    elif live:
        out.append("The `va` stage is still open: its record is written once every pipeline "
                   "has ended.")
    else:
        out.append("No `va` stage record: the app stopped (or was killed) before the "
                   "pipelines finished, so these rows come from the runner logs alone.")
    if short:
        out.append(f"**Input not fully processed** ({'; '.join(short)}): the pipeline ended "
                   "before the end of its input, so its duration is shorter than the input "
                   "length.")
    out.append("Duration is wall time from PLAYING to the runner's last log line. Input length "
               "is the MP4 file's own length; processed is the input position of the last "
               "frame with published results, so a run that reached EOS processed its whole "
               "input. Speed is processed input per second of processing (about 1.0x when the "
               "stream output plays in real time, higher for headless sessions with the stream "
               "output off).")
    out.append("FPS is the pipeline's gvafpscounter average (where it sits in the pipeline "
               "differs per pipeline); output records count one line per frame with "
               "published metadata, per result file.")
    return "\n".join(out) + "\n"


def session_section(session: str, stages: list, calls: list, va_runs: list = (),
                    row: dict = None) -> str:
    rows = lanes(stages, calls, va_runs)
    # Still running when the report was made: a VA runner is still writing its
    # log, or the session store says so and nothing shows the app stopped.
    stopped = _stopped_evidence(stages, va_runs)
    live = any(r["status"] == "running" for r in va_runs) or bool(
        row and row.get("state") == "running" and not stopped)
    if not rows:
        return "\n".join([f"## Session `{session}`", "", *_state_lines(row, live, stopped),
                          "", "No timed records.", ""])
    t0 = min(b[0] for _, _, blocks in rows for b in blocks)
    t1 = max(b[1] for _, _, blocks in rows for b in blocks)
    out = [f"## Session `{session}`", ""]
    if live:
        now = datetime.now().astimezone()
        out += [f"> **In progress.** Snapshot taken {now.isoformat(timespec='seconds')}, "
                f"{now.timestamp() - t0:,.0f} s after the session started. Stages and VA "
                "pipelines still running show their time so far, and stages after them "
                "have not started; re-run the report once the session has completed.", ""]
    out += [*_state_lines(row, live, stopped),
            f"Workflow window{' so far' if live else ''}: **{t1 - t0:,.1f} s**, from "
            f"{datetime.fromtimestamp(t0).astimezone().isoformat(timespec='seconds')} "
            "to the end of the last stage, call or VA pipeline."]
    transcribe = [span for r in stages if r.get("stage") == "transcribe"
                  if (span := stage_interval(r))]
    if transcribe:
        tail = t1 - max(end for _, end in transcribe)
        out.append(f"After transcription ended, the remaining outputs took **{tail:,.1f} s**"
                   f"{' so far' if live else ''}.")
    out += ["", "### Timeline", "",
            "Lanes run in parallel; overlapping stages are expected, so stage durations "
            "do not add up to the workflow window. `*` marks a stage seen only through "
            "its model calls (first call to last call); `va <pipeline>` lanes come from "
            "the VA runner logs, one bar per attempt.", "",
            mermaid(session, rows), "",
            "Plain-text view (`█` completed, `░` derived from model calls, `X` failed, "
            "interrupted or cancelled):", "",
            ascii_chart(rows, t0, t1), "",
            "### Stages", "",
            "| Stage | Status | Start (s) | End (s) | Duration (s) | Model calls | "
            "Model time (s) | Queue wait (s) |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for label, kind, blocks in rows:
        if kind in ("calls", "va"):
            continue
        stage = None if label == "unattributed" else label
        for (start, end, status), attempt in zip(blocks,
                                                 _calls_per_attempt(calls, stage, blocks)):
            n, model, queued = _totals(attempt)
            name, shown = (f"{label} *", "from calls") if kind == "derived" else (label, status)
            out.append(f"| {name} | {shown} | {start - t0:,.1f} | {end - t0:,.1f} | "
                       f"{end - start:,.1f} | {n} | {model:,.1f} | {queued:,.1f} |")
    out.append("")
    text_gen = session_text_gen_table(calls, t0)
    if text_gen:
        out += ["### Text generation", "",
                f"One row per VLM/LLM call; start is its submission. {_TEXT_GEN_LEGEND}", "",
                text_gen]
    requested = requested_va_pipelines(row)
    if va_runs or requested:
        out += ["### Video analytics", "", va_table(va_runs, stages, t0, live, requested)]
    return "\n".join(out)


def _count(d) -> str:
    return f"{d['count']}{' ⚠' if d.get('small_sample') else ''}"


def calls_table(calls: list) -> str:
    groups = call_groups(calls, ("capability", "stage", "model", "precision", "device",
                                 "decoding"))
    if not groups:
        return "No model calls recorded.\n"
    out = ["| Capability | Stage | Model | Precision | Device | Decoding | OK | "
           "Latency mean / p50 / p95 (ms) | Queue wait p95 (ms) | Failed / cancelled |",
           "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for g in groups:
        lat = g["latency_ms"]
        out.append(
            f"| {g['capability']} | {g['stage'] or '—'} | {g['model'] or '—'} | "
            f"{g['precision'] or '—'} | {g['device'] or '—'} | {g['decoding'] or '—'} | "
            f"{_count(lat)} | {_num(lat['mean'], 0)} / {_num(lat['p50'], 0)} / "
            f"{_num(lat['p95'], 0)} | {_num(g['queue_wait_ms']['p95'], 0)} | "
            f"{g['failed_count']} / {g['cancelled_count']} |")
    return "\n".join(out) + "\n"


_TEXT_GEN = "text_gen"
_MODEL_KEYS = ("model", "precision", "device", "decoding")
_TEXT_GEN_LEGEND = (
    "TTFT (model time to first token) and TPOT (time per output token after the "
    "first) are runtime-reported; decode tok/s is 1000 / TPOT, prefill tok/s is prompt "
    "tokens per second of TTFT, and output tok/s is generated tokens per second of call "
    "latency (prefill included). Accepted / drafted counts DFlash draft tokens.")


def _acceptance(acc: dict, calls: int) -> str:
    """Accepted / drafted (rate); paired samples only, so it may cover fewer calls."""
    if not acc["calls"]:
        return "—"
    text = f"{_num(acc['accepted'], 0)} / {_num(acc['drafted'], 0)} ({acc['rate']:.0%})"
    return text + (f", {acc['calls']} of {calls} calls" if acc["calls"] < calls else "")


def text_gen_table(calls: list) -> str:
    """Per stage, then all stages, for every text-generation model identity."""
    calls = [r for r in calls if r.get("capability") == _TEXT_GEN]
    by_stage = call_groups(calls, (*_MODEL_KEYS, "stage"))
    if not by_stage:
        return "No text-generation calls recorded.\n"
    overall = {tuple(g[k] for k in _MODEL_KEYS): g for g in call_groups(calls, _MODEL_KEYS)}
    out = ["| Stage | Model | Precision | Device | Decoding | OK | TTFT p50 / p95 (ms) | "
           "TPOT p50 / p95 (ms) | Decode p50 (tok/s) | Output p50 (tok/s) | "
           "Prefill p50 (tok/s) | Tokens in / out | Accepted / drafted | Failed / cancelled |",
           "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
           "---: | ---: |"]

    def row(g, stage):
        ok = g["latency_ms"]["count"]
        return (f"| {stage} | {g['model'] or '—'} | {g['precision'] or '—'} | "
                f"{g['device'] or '—'} | {g['decoding'] or '—'} | {_count(g['latency_ms'])} | "
                f"{_num(g['ttft_ms']['p50'], 0)} / {_num(g['ttft_ms']['p95'], 0)} | "
                f"{_num(g['tpot_ms']['p50'])} / {_num(g['tpot_ms']['p95'])} | "
                f"{_num(g['decode_tps']['p50'])} | {_num(g['output_tps']['p50'])} | "
                f"{_num(g['prefill_tps']['p50'], 0)} | "
                f"{_num(g['input_tokens']['total'], 0)} / {_num(g['output_tokens']['total'], 0)} | "
                f"{_acceptance(g['acceptance'], ok)} | "
                f"{g['failed_count']} / {g['cancelled_count']} |")

    # Grouped by identity first, so each model's stages are contiguous.
    for identity, rows in groupby(by_stage, key=lambda g: tuple(g[k] for k in _MODEL_KEYS)):
        rows = list(rows)
        out += [row(g, g["stage"] or "—") for g in rows]
        if len(rows) > 1:
            out.append(row(overall[identity], "**all stages**"))
    return "\n".join(out) + "\n"


def _identity(r) -> str:
    return " ".join(str(r[k]) for k in _MODEL_KEYS if r.get(k))


def session_text_gen_table(calls: list, t0: float) -> str:
    """One row per text-generation call of a session, in submission order."""
    calls = sorted((r for r in calls if r.get("capability") == _TEXT_GEN),
                   key=lambda r: _ts(r.get("submitted_at")) or 0.0)
    if not calls:
        return ""
    identities = sorted({_identity(r) for r in calls})
    many = len(identities) > 1
    out = [] if many else [f"Model: `{identities[0]}`.", ""]
    out += ["| Stage | Start (s) | Status | " + ("Model | " if many else "")
            + "Tokens in / out | Queue wait (ms) | Latency (s) | TTFT (ms) | TPOT (ms) | "
            "Decode (tok/s) | Output (tok/s) | Prefill (tok/s) | Accepted / drafted |",
            "| --- | ---: | --- | " + ("--- | " if many else "")
            + "---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in calls:
        submitted, rates = _ts(r.get("submitted_at")), call_rates(r)
        latency = r.get("latency_ms")
        drafted, accepted = r.get("draft_tokens"), r.get("accepted_tokens")
        acceptance = (f"{accepted:,} / {drafted:,} ({accepted / drafted:.0%})"
                      if isinstance(accepted, int) and isinstance(drafted, int) and drafted
                      else "—")
        out.append(
            f"| {r.get('stage') or '—'} | "
            f"{_num(submitted - t0 if submitted is not None else None)} | "
            f"{r.get('status') or '—'} | " + (f"{_identity(r)} | " if many else "")
            + f"{_num(r.get('input_tokens'), 0)} / {_num(r.get('output_tokens'), 0)} | "
            f"{_num(r.get('queue_wait_ms'), 0)} | "
            f"{_num(latency / 1000.0 if isinstance(latency, (int, float)) else None)} | "
            f"{_num(r.get('ttft_ms'), 0)} | {_num(r.get('tpot_ms'))} | "
            f"{_num(rates['decode_tps'])} | {_num(rates['output_tps'])} | "
            f"{_num(rates['prefill_tps'], 0)} | {acceptance} |")
    return "\n".join(out) + "\n"


def stages_table(stages: list) -> str:
    out = ["| Stage | Completed | Mean (s) | p50 (s) | p95 (s) | Min / max (s) | "
           "Failed | Interrupted |",
           "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for g in stage_groups(stages):
        d = g["duration_sec"]
        out.append(f"| {g['stage']} | {_count(d)} | "
                   f"{_num(d['mean'])} | {_num(d['p50'])} | {_num(d['p95'])} | "
                   f"{_num(d['min'])} / {_num(d['max'])} | {g['failed_count']} | "
                   f"{g['cancelled_count']} |")
    return "\n".join(out) + "\n"


def report(sources: list, session: str = None) -> str:
    stages, calls, va_runs, rows = [], [], [], {}
    for source in sources:
        s, c = load(Path(source))
        stages += s
        calls += c
        session_dir = _session_dir(Path(source))
        if session_dir:
            sid = _session_id(session_dir, s + c)
            va_runs += load_va(session_dir, sid)
            rows.update(load_session_rows(session_dir, sid))
    if session:
        stages = [r for r in stages if r.get("session_id") == session]
        calls = [r for r in calls if r.get("session_id") == session]
        va_runs = [r for r in va_runs if r.get("session_id") == session]
    sessions = sorted({r.get("session_id") for r in stages + calls + va_runs
                       if r.get("session_id")})
    out = ["# Processing-Time Report", "",
           f"Sources: {', '.join(f'`{s}`' for s in sources)}. "
           f"{len(sessions)} session(s), {len(stages)} stage record(s), "
           f"{len(calls)} model call(s), {len(va_runs)} VA pipeline run(s). Stage times "
           "are seconds, model-call times milliseconds; `—` is unavailable, never zero.", ""]
    for sid in sessions:
        out.append(session_section(
            sid, [r for r in stages if r.get("session_id") == sid],
            [r for r in calls if r.get("session_id") == sid],
            [r for r in va_runs if r.get("session_id") == sid], rows.get(sid)))
    unattributed = [r for r in calls if not r.get("session_id")]
    out += ["## Stage Durations", "",
            "Completed attempts only; failed and interrupted attempts are counted "
            "separately. Percentiles are nearest-rank; ⚠ marks fewer than 20 samples "
            "(an observation, not a distribution).", "",
            stages_table(stages),
            "## Model Calls", "",
            "Latency runs from admission to the end of the call; queue wait is "
            "separate. Distributions cover successful calls. "
            f"{len(unattributed)} call(s) had no session (for example chat API calls).",
            "", calls_table(calls),
            "## Text Generation", "",
            "VLM/LLM performance per stage, and across all stages for each model. "
            f"{_TEXT_GEN_LEGEND} Token counts are totals over successful calls.", "",
            text_gen_table(calls)]
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sources", nargs="+",
                        help="session directory or JSON from GET /processing-stats?raw=true")
    parser.add_argument("-o", "--output", help="write the report here (default: stdout)")
    parser.add_argument("--session", help="only this session id")
    args = parser.parse_args(argv)
    text = report(args.sources, args.session)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote {args.output}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Long transcripts are summarized in segments; short ones must not be."""

import os
import re
import sys
from unittest.mock import patch

_SC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)

from components.summarizer_component import SummarizerComponent
from utils import text_chunker as tc


class FakeTokenizer:
    def encode(self, text):
        return text.split()


class FakeHandler:
    """Records every generate() call and streams a canned summary."""

    SUMMARY_TOKENS = ["## Teacher Summary\n", "- a point\n", "## Keywords\n", "- term\n"]
    NOTE = "TOPICS: supply and demand\nKEYWORDS: elasticity"

    def __init__(self, note=None):
        self.tokenizer = FakeTokenizer()
        self.calls = []
        # Long notes are what push the reduce prompt over budget and force a
        # fold, so tests that need one hand in their own.
        self.note = note or self.NOTE
        # generate() calls and yielded items in the order they happened, so a
        # test can tell "progress, work, progress" from "all progress up front".
        self.timeline = []
        # Whatever reached performance_metrics.csv, in call order.
        self.metrics = []

    def generate(self, prompt=None, *, messages=None, stream=True,
                 max_new_tokens=None, **kwargs):
        call = {"messages": messages, "stream": stream,
                "max_new_tokens": max_new_tokens}
        self.calls.append(call)
        self.timeline.append(("generate", call))
        if not stream:
            return self.note
        return iter(self.SUMMARY_TOKENS)

    @property
    def map_calls(self):
        return [c for c in self.calls if c["stream"] is False]

    @property
    def stream_calls(self):
        return [c for c in self.calls if c["stream"] is True]


def _text(call):
    return "\n".join(m["content"] for m in call["messages"])


def _transcript(n_lines):
    return "\n".join(
        f"[{i * 10}-{i * 10 + 10}] " + " ".join(f"w{i}x{j}" for j in range(20))
        for i in range(n_lines)
    )


def _speaker_lines(n_lines):
    """The dialog transcript: speakers, and no timestamps of its own."""
    return "\n".join(
        f"{'Teacher' if i % 3 else 'Student'}: "
        + " ".join(f"w{i}x{j}" for j in range(20))
        for i in range(n_lines)
    )


def _component(handler=None):
    """A component wired to ``handler``, with nothing loaded from disk."""
    handler = handler or FakeHandler()
    with patch("components.summarizer_component.ModelManager") as mock_mm:
        mock_mm.instance.return_value.text_gen.return_value = handler
        return SummarizerComponent(session_id="s1", mode="dialog")


def _run(transcript, budget, board_text="", mode="dialog", note=None):
    """Drive process() to completion, returning (component, handler, items, saved)."""
    handler = FakeHandler(note=note)
    saved = []

    with patch("components.summarizer_component.ModelManager") as mock_mm:
        mock_mm.instance.return_value.text_gen.return_value = handler
        component = SummarizerComponent(session_id="s1", mode=mode)

    with patch.object(SummarizerComponent, "_load_input_text", return_value=transcript), \
         patch.object(SummarizerComponent, "_load_board_ocr_text", return_value=board_text), \
         patch("components.summarizer_component.RuntimeConfig") as rc, \
         patch.object(tc, "resolve_budget", return_value=budget), \
         patch("components.summarizer_component.StorageManager") as sm:
        rc.get_section.return_value = {"location": "storage/", "name": "p"}
        sm.save.side_effect = \
            lambda path, data, append=False: saved.append(data) if append else None
        sm.update_csv.side_effect = \
            lambda path, new_data: handler.metrics.append(new_data)
        # Consumed one item at a time: draining the generator into a list first
        # would lose where each yield falls between the generate() calls.
        items = []
        for item in component.process(None):
            handler.timeline.append(("yield", item))
            items.append(item)

    return component, handler, items, "".join(saved)


def _tokens(items):
    return [i for i in items if isinstance(i, str)]


def _progress(items):
    return [i for i in items
            if isinstance(i, dict) and i.get("event") == "progress"]


# ------------------------------------------------------------ single-shot path

def test_short_transcript_makes_exactly_one_call():
    _, handler, items, saved = _run(_transcript(10), budget=100_000)

    assert len(handler.calls) == 1
    assert handler.calls[0]["stream"] is True
    assert _tokens(items) == FakeHandler.SUMMARY_TOKENS
    assert saved == "".join(FakeHandler.SUMMARY_TOKENS)
    assert _progress(items) == []


def test_short_transcript_messages_are_unchanged():
    """The single-shot messages must match the pre-chunking build."""
    transcript = _transcript(10)
    component, handler, _, _ = _run(transcript, budget=100_000)

    assert handler.calls[0]["messages"] == component._get_message(transcript, "")


def test_board_text_reaches_the_single_shot_prompt():
    _, handler, _, _ = _run(_transcript(10), budget=100_000, board_text="SLIDE ONE")
    text = _text(handler.calls[0])
    assert "SLIDE ONE" in text
    assert "[BOARD CONTENT]" in text


def test_board_text_counts_against_the_budget():
    """Board OCR rides along in the reduce prompt, so it has to be reserved too."""
    # ~20k tokens of transcript: comfortably inside a 30k budget on its own,
    # but not once an 8k board text is reserved alongside the answer.
    transcript = _transcript(910)
    board = " ".join(f"slide_word_{i}" for i in range(8_000))

    _, plain, _, _ = _run(transcript, budget=30_000)
    assert not plain.map_calls, "the transcript alone fits in one call"

    _, with_board, _, _ = _run(transcript, budget=30_000, board_text=board)
    assert with_board.map_calls, "a large board text must force segmentation"


def test_system_prompt_counts_against_the_budget():
    """The instructions occupy the same cache as the transcript."""
    from utils.config_loader import config

    component = _component()
    answer_only = int(config.models.text_gen.max_new_tokens)
    assert component._call_overhead("") > answer_only, \
        "the reserve must include the rendered system prompt"


# ------------------------------------------------------------- board share

def _board(n_lines, words=20):
    return "\n".join(
        " ".join(f"board_{i}_{j}" for j in range(words)) for i in range(n_lines)
    )


def _board_calls(handler):
    from components.board_ocr.board_ocr_service import board_summary_system_prompt
    from utils.config_loader import config

    system = board_summary_system_prompt(config.app.language)
    return [c for c in handler.calls if c["messages"][0]["content"] == system]


def test_a_board_within_its_share_is_sent_as_is():
    board = _board(5)
    _, handler, items, _ = _run(_transcript(10), budget=30_000, board_text=board)

    assert not _board_calls(handler), "a board that fits must not be condensed"
    assert board in _text(handler.stream_calls[0])
    assert not [p for p in _progress(items) if p["stage"] == "board"]


def test_a_board_over_its_share_is_condensed_before_the_lesson_call():
    """Board OCR used to ride whole in the lesson call, however large: 50k tokens
    of it made a 60k-token prompt against a 24k budget."""
    board = _board(2_000)  # ~42k tokens
    component, handler, items, _ = _run(_transcript(10), budget=30_000, board_text=board)

    board_calls = _board_calls(handler)
    assert board_calls, "an oversized board must be condensed"
    lesson = _text(handler.stream_calls[0])
    assert "board_1999_0" not in lesson, "the raw board must not reach the lesson call"
    assert FakeHandler.NOTE in lesson, "the condensed notes stand in for it"

    # The notes' answer limits add up to the share, so they fit by construction.
    cap = component._board_cap(30_000)
    assert sum(c["max_new_tokens"] for c in board_calls) <= cap
    for call in board_calls:
        prompt = sum(len(m["content"].split()) for m in call["messages"])
        assert prompt + call["max_new_tokens"] <= 30_000, "a condensing call is over budget"


def test_board_progress_precedes_each_condensing_call():
    _, handler, _, _ = _run(_transcript(10), budget=30_000, board_text=_board(2_000))

    events = [e for e in handler.timeline
              if e[0] == "generate" and e[1] in _board_calls(handler)
              or e[0] == "yield" and isinstance(e[1], dict) and e[1].get("stage") == "board"]
    kinds = [e[0] for e in events]
    assert kinds == ["yield", "generate"] * (len(kinds) // 2), kinds


def test_the_lesson_call_fits_the_budget_however_large_the_board():
    from utils.config_loader import config

    answer = int(config.models.text_gen.max_new_tokens)
    for n_lines in (50, 500, 5_000):
        _, handler, _, _ = _run(_transcript(300), budget=30_000, board_text=_board(n_lines))
        lesson = handler.stream_calls[0]
        prompt = sum(len(m["content"].split()) for m in lesson["messages"])
        assert prompt + answer <= 30_000, f"{n_lines}-line board: {prompt} + {answer}"


def test_the_board_share_keeps_the_lesson_reserve_within_half_the_budget():
    """Past half, usable_tokens caps the reserve and the plan under-counts the call."""
    component = _component()
    for budget in (24_000, 30_000, 100_000):
        cap = component._board_cap(budget)
        assert component._call_overhead(" ".join(["w"] * cap)) <= budget // 2, budget


def test_condensed_notes_over_the_share_are_trimmed_to_it():
    """A model that writes past its answer limit must not reopen the gap."""
    long_note = "\n".join(" ".join(f"n{i}_{j}" for j in range(50)) for i in range(400))
    component, handler, _, _ = _run(_transcript(10), budget=30_000,
                                    board_text=_board(2_000), note=long_note)

    assert _board_calls(handler), "the board must have been condensed"
    lesson = handler.stream_calls[0]["messages"][1]["content"]
    board_part = lesson.split("[BOARD CONTENT]\n", 1)[1]
    assert len(board_part.split()) <= component._board_cap(30_000)
    assert component._call_overhead(board_part) <= 30_000 // 2


def test_trimming_keeps_whole_lines_and_cuts_a_single_long_one():
    trim = SummarizerComponent._trim_to_tokens
    tok = FakeTokenizer()
    assert trim("a b\nc d\ne f", 4, tok) == "a b\nc d"
    one = trim(" ".join(f"w{i}" for i in range(100)), 10, tok)
    assert 0 < len(one.split()) <= 10


# ------------------------------------------------------------- answer limit

def _warnings(items):
    return [i for i in items if isinstance(i, dict) and i.get("event") == "warning"]


def _lesson_fits(handler, budget):
    lesson = handler.stream_calls[0]
    prompt = sum(len(m["content"].split()) for m in lesson["messages"])
    return prompt + lesson["max_new_tokens"] <= budget


def test_a_configured_answer_that_fits_is_used_as_is():
    from utils.config_loader import config

    _, handler, items, _ = _run(_transcript(10), budget=30_000, board_text=_board(5))
    assert handler.stream_calls[0]["max_new_tokens"] == int(config.models.text_gen.max_new_tokens)
    assert not [w for w in _warnings(items) if w["code"] == "answer_lowered"]


def test_a_configured_answer_too_large_falls_back_to_the_default():
    from utils.config_loader import config

    with patch.object(config.models.text_gen, "max_new_tokens", 20_000):
        component, handler, items, _ = _run(_transcript(10), budget=30_000,
                                            board_text=_board(2_000))

    assert handler.stream_calls[0]["max_new_tokens"] == 5120
    lowered = [w for w in _warnings(items) if w["code"] == "answer_lowered"]
    assert [(w["configured"], w["used"]) for w in lowered] == [(20_000, 5120)]
    assert _lesson_fits(handler, 30_000)


def test_an_answer_lowered_below_the_default_when_even_that_does_not_fit():
    """A small device budget: the default alone takes more than half of it."""
    component, handler, items, _ = _run(_transcript(10), budget=8_000,
                                        board_text=_board(400))

    used = handler.stream_calls[0]["max_new_tokens"]
    assert used < 5120
    lowered = [w for w in _warnings(items) if w["code"] == "answer_lowered"]
    assert lowered and lowered[0]["used"] == used
    assert not [w for w in _warnings(items) if w["code"] == "reserve_capped"], \
        "the lowered answer must keep the reserve within half the budget"
    assert _lesson_fits(handler, 8_000)


def test_a_transcript_that_fits_is_planned_as_no_chunks_at_all():
    """The caller distinguishes "one call" by an empty chunk list, not by len 1."""
    component = _component()
    with patch.object(tc, "resolve_budget", return_value=100_000):
        chunks, budget, reserve, total = component._plan_chunks(_transcript(10), "")

    assert chunks == []
    assert budget == 100_000
    assert reserve == component._call_overhead("")
    assert total == 10 * 22, "22 tokens a line, counted once"


# -------------------------------------------------------------- map-reduce path

def test_long_transcript_maps_then_reduces():
    _, handler, items, saved = _run(_transcript(400), budget=2_000)

    assert len(handler.map_calls) >= 2, "expected a map call per chunk"
    assert len(handler.stream_calls) == 1, "exactly one streamed reduce call"
    assert handler.calls[-1]["stream"] is True


def test_only_the_reduce_output_reaches_the_client_and_disk():
    _, handler, items, saved = _run(_transcript(400), budget=2_000)

    assert _tokens(items) == FakeHandler.SUMMARY_TOKENS
    assert saved == "".join(FakeHandler.SUMMARY_TOKENS)
    assert "TOPICS: supply and demand" not in saved


def test_progress_is_emitted_and_never_written_to_disk():
    _, handler, items, saved = _run(_transcript(400), budget=2_000)

    progress = _progress(items)
    assert progress, "map stage must report progress"
    assert all(p["event"] == "progress" for p in progress)

    stages = {p["stage"] for p in progress}
    assert "map" in stages and "reduce" in stages

    map_progress = [p for p in progress if p["stage"] == "map"]
    assert [p["chunk"] for p in map_progress] == list(range(1, len(map_progress) + 1))
    assert all(p["chunks"] == len(map_progress) for p in map_progress)

    for p in progress:
        assert str(p) not in saved


def test_map_progress_is_interleaved_with_its_own_generate_call():
    """Each segment is announced right before it is summarized.

    Hoisting the yields out of the map loop keeps the chunk numbers in order,
    so number-only assertions stay green while the client jumps to "part 7 of
    7" and waits there for the whole map stage. Pair every progress event with
    the generate() call it precedes instead.
    """
    _, handler, items, _ = _run(_transcript(400), budget=2_000)

    map_progress = [p for p in _progress(items) if p["stage"] == "map"]
    assert len(map_progress) >= 2, "this transcript must span several segments"

    announced, paired = None, []
    for kind, payload in handler.timeline:
        if kind == "yield" and isinstance(payload, dict) and payload["stage"] == "map":
            assert announced is None, (
                f"segment {payload['chunk']} was announced while segment "
                f"{announced['chunk']} had not been summarized yet: progress "
                "is being emitted outside the map loop"
            )
            announced = payload
        elif kind == "generate" and announced is not None:
            header = f"[SEGMENT {announced['chunk']}/{announced['chunks']}]"
            assert header in _text(payload), (
                f"the call after progress for {header} summarized a different "
                "segment"
            )
            paired.append(announced)
            announced = None

    assert announced is None, "the last segment was announced but never summarized"
    assert paired == map_progress


def test_reduce_progress_precedes_the_streamed_call():
    """The reduce stage is announced before it starts, not after it streams."""
    _, handler, _, _ = _run(_transcript(400), budget=2_000)

    kinds = [
        "reduce-progress" if kind == "yield" and isinstance(payload, dict)
        and payload["stage"] == "reduce"
        else "stream" if kind == "generate" and payload["stream"]
        else "map-call" if kind == "generate" else None
        for kind, payload in handler.timeline
    ]
    kinds = [k for k in kinds if k]

    assert kinds.count("reduce-progress") == 1
    assert kinds.index("reduce-progress") > kinds.index("map-call"), (
        "reduce must be announced only once the map stage is done"
    )
    assert kinds.index("reduce-progress") < kinds.index("stream"), (
        "reduce progress must reach the client before the reduce call blocks"
    )


def test_map_calls_are_capped_and_reduce_is_not():
    component, handler, _, _ = _run(_transcript(400), budget=2_000)

    for call in handler.map_calls:
        assert call["max_new_tokens"] == 1536, "map notes must be length-capped"
    # The reduce call writes the whole summary: it gets the lesson answer limit,
    # passed explicitly so the call is the size its reserve was planned at.
    assert handler.stream_calls[0]["max_new_tokens"] == component._lesson_answer_tokens()


def test_reduce_prompt_carries_the_notes_not_the_transcript():
    _, handler, _, _ = _run(_transcript(400), budget=2_000)

    reduce_text = _text(handler.stream_calls[0])
    assert "TOPICS: supply and demand" in reduce_text
    assert "[SEGMENT 1/" in reduce_text
    assert "w399x0" not in reduce_text


def test_the_reduce_prompt_opens_with_its_own_instruction():
    """The notes are not a transcript, so reduce is told what it is reading."""
    from utils.config_loader import config
    from utils.prompt_loader import load_prompt

    _, handler, _, _ = _run(_transcript(400), budget=2_000)
    instruction = load_prompt("summarizer", config.app.language, "reduce_instruction")

    body = handler.stream_calls[0]["messages"][-1]["content"]
    assert body.startswith(instruction.strip())


def test_the_map_prompt_states_the_mode_it_is_reading_for():
    """A teacher-mode run must not have its segments summarized for dialog."""
    _, teacher, _, _ = _run(_transcript(400), budget=2_000, mode="teacher")
    _, dialog, _, _ = _run(_transcript(400), budget=2_000, mode="dialog")

    assert "{focus}" not in _text(teacher.map_calls[0]), "the focus was never filled in"
    assert _text(teacher.map_calls[0]) != _text(dialog.map_calls[0])


def test_a_timestamped_transcript_labels_its_segments_with_minutes():
    """Teacher mode reads a timestamped file, so its notes can carry spans."""
    _, handler, _, _ = _run(_transcript(400), budget=2_000, mode="teacher")

    assert handler.map_calls, "a 400-turn lesson must be segmented at this budget"
    headers = [c["messages"][-1]["content"].splitlines()[0] for c in handler.map_calls]
    assert all(re.fullmatch(r"\[SEGMENT \d+/\d+\] \d\d:\d\d-\d\d:\d\d", h)
               for h in headers), f"expected mm:ss-mm:ss spans, got {headers}"

    # And the reduce prompt keeps them, so it can order the notes.
    assert "[SEGMENT 1/" in _text(handler.stream_calls[0])
    assert "00:00-" in _text(handler.stream_calls[0])


def test_an_untimestamped_transcript_still_numbers_its_segments():
    """The speaker transcript has no times; an index is all the map prompt needs."""
    _, handler, _, _ = _run(_speaker_lines(400), budget=2_000)

    assert handler.map_calls
    headers = [c["messages"][-1]["content"].splitlines()[0] for c in handler.map_calls]
    assert all(re.fullmatch(r"\[SEGMENT \d+/\d+\]", h) for h in headers), \
        f"a transcript with no times must not be given invented ones: {headers}"
    assert "Teacher:" in _text(handler.map_calls[0])


def test_a_chunk_is_not_charged_for_the_full_summary():
    """Chunk size follows the map stage's costs, not the reduce stage's."""
    component = _component()
    assert 0 < component._stage_reserve() < component._call_overhead("")


def test_segments_may_be_larger_than_one_whole_lesson_call_allows():
    """The cheaper per-segment reserve is the point: fewer, fatter map calls."""
    component = _component()
    budget = 8_000
    with patch.object(tc, "resolve_budget", return_value=budget):
        chunks, _, reserve, _ = component._plan_chunks(_transcript(400), "")

    assert len(chunks) > 1
    assert max(c.tokens for c in chunks) > tc.usable_tokens(budget, reserve)


def test_the_recorded_input_tokens_do_not_double_count_the_overlap():
    """Segments repeat their trailing lines; the transcript was read only once."""
    _, handler, _, _ = _run(_transcript(400), budget=2_000)

    metrics = handler.metrics[-1]
    assert metrics["performance.summary_strategy"] == "map_reduce"
    assert metrics["performance.summary_chunks"] == len(handler.map_calls)
    assert metrics["performance.summary_input_tokens"] == 400 * 22


def test_a_single_shot_run_is_recorded_as_one():
    _, handler, _, _ = _run(_transcript(10), budget=100_000)

    metrics = handler.metrics[-1]
    assert metrics["performance.summary_strategy"] == "single_shot"
    assert metrics["performance.summary_chunks"] == 1
    assert metrics["performance.summary_map_time"] == "0.0s"


def test_metrics_are_written_even_when_the_reduce_call_dies():
    """The metrics are the only record of a summary that failed halfway."""
    handler = FakeHandler()
    handler.generate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("device lost"))

    with patch("components.summarizer_component.ModelManager") as mock_mm:
        mock_mm.instance.return_value.text_gen.return_value = handler
        component = SummarizerComponent(session_id="s1", mode="dialog")

    with patch.object(SummarizerComponent, "_load_input_text",
                      return_value=_transcript(10)), \
         patch.object(SummarizerComponent, "_load_board_ocr_text", return_value=""), \
         patch("components.summarizer_component.RuntimeConfig") as rc, \
         patch.object(tc, "resolve_budget", return_value=100_000), \
         patch("components.summarizer_component.StorageManager") as sm:
        rc.get_section.return_value = {"location": "storage/", "name": "p"}
        sm.update_csv.side_effect = \
            lambda path, new_data: handler.metrics.append(new_data)
        try:
            list(component.process(None))
        except RuntimeError:
            pass
        else:
            raise AssertionError("the failure must not be swallowed")

    assert handler.metrics, "a run that died still has to leave its metrics"


# ------------------------------------------------------------------ the fold

def _oversized_notes(n=40):
    return [
        f"[SEGMENT {i + 1}/{n}] {i * 2:02d}:00-{i * 2 + 2:02d}:00\n"
        + " ".join(f"n{i}w{j}" for j in range(200))
        for i in range(n)
    ]


def _fold(component, notes, budget=4_000, max_new_tokens=256, reserve=1_000):
    """Drive the fold generator to the end -> (folded notes, progress events)."""
    gen = component._fold_notes(notes, budget=budget,
                                max_new_tokens=max_new_tokens, reserve=reserve)
    events = []
    while True:
        try:
            event = next(gen)
        except StopIteration as stop:
            return stop.value, events
        # Recorded on the handler's timeline, as _run does, so the ordering
        # against generate() survives into the interleaving test.
        component.summarizer.timeline.append(("yield", event))
        events.append(event)


def test_folded_note_groups_do_not_pose_as_lesson_segments():
    """[SEGMENT n/m] numbers the lesson; a fold groups notes, so it says NOTES."""
    component = _component()
    notes = _oversized_notes()
    folded, _ = _fold(component, notes)

    assert len(folded) < len(notes), "oversized notes must be folded"
    assert all(n.startswith("[NOTES ") for n in folded)

    # The prompt must carry the same label as the answer, or the fold tells the
    # model it is reading segment 1 of 3 of the lesson.
    fold_calls = component.summarizer.map_calls
    assert fold_calls
    assert all(c["messages"][-1]["content"].startswith("[NOTES ") for c in fold_calls)


def test_folding_keeps_the_span_the_notes_covered():
    """A fold rewrites its input, so the times have to be lifted out first."""
    component = _component()
    folded, _ = _fold(component, _oversized_notes())

    headers = [n.splitlines()[0] for n in folded]
    assert all(re.fullmatch(r"\[NOTES \d+/\d+\] \d\d:\d\d-\d\d:\d\d", h)
               for h in headers), f"folded notes lost their spans: {headers}"

    # Between them the groups span the whole lesson, whatever the group size.
    assert "] 00:00-" in headers[0]
    assert headers[-1].endswith("-80:00")


def test_notes_without_spans_are_folded_under_a_bare_label():
    """A dialog run's notes carry no times, and must not be given invented ones."""
    component = _component()
    notes = [f"[SEGMENT {i + 1}/40]\n" + " ".join(f"n{i}w{j}" for j in range(200))
             for i in range(40)]
    folded, _ = _fold(component, notes)

    assert folded
    assert all(re.fullmatch(r"\[NOTES \d+/\d+\]", n.splitlines()[0]) for n in folded)


def test_folding_reports_progress_for_every_group():
    """A fold runs model calls of its own, so it must not go silent."""
    component = _component()
    folded, events = _fold(component, _oversized_notes())

    assert events, "folding must report progress"
    assert all(e["event"] == "progress" and e["stage"] == "fold" for e in events)

    # The counter restarts per round, so check the numbering round by round.
    for round_no in sorted({e["round"] for e in events}):
        this_round = [e for e in events if e["round"] == round_no]
        assert [e["chunk"] for e in this_round] == list(range(1, len(this_round) + 1))
        assert all(e["chunks"] == len(this_round) for e in this_round)

    assert len(events) >= len(folded)


def test_fold_progress_is_interleaved_with_its_own_generate_call():
    """Same rule as the map loop: announce a group, then summarize it."""
    component = _component()
    _fold(component, _oversized_notes())

    announced = None
    for kind, payload in component.summarizer.timeline:
        if kind == "yield":
            assert announced is None, (
                f"group {payload['chunk']} was announced while group "
                f"{announced['chunk']} had not been folded yet"
            )
            announced = payload
        elif kind == "generate":
            assert announced is not None, (
                "a fold call ran with no progress event announcing it"
            )
            header = f"[NOTES {announced['chunk']}/{announced['chunks']}]"
            user_msg = payload["messages"][-1]["content"]
            assert user_msg.startswith(header), (
                f"the call after progress for {header} folded a different group"
            )
            announced = None

    assert announced is None, "the last group was announced but never folded"


def test_no_progress_when_the_notes_already_fit():
    """A fold that does nothing must not emit a stage the client would show."""
    component = _component()
    notes = ["[SEGMENT 1/1] 00:00-02:00\nshort"]
    folded, events = _fold(component, notes, budget=100_000, reserve=1_000)

    assert folded == notes
    assert events == []
    assert component.summarizer.calls == []


def test_folding_gives_up_loudly_rather_than_looping(caplog):
    """Two rounds is the cap; notes that still do not fit are reported."""
    component = _component(FakeHandler(note=" ".join(f"x{i}" for i in range(3_000))))

    with caplog.at_level("WARNING"):
        folded, _ = _fold(component, _oversized_notes(n=60), budget=2_000, reserve=500)

    assert folded, "the run continues with an over-budget reduce prompt"
    assert any("fold round(s)" in r.message for r in caplog.records)


# ------------------------------------------------- fold inside the full stream

def _long_note():
    """A map note fat enough to push the reduce prompt over a 2k budget."""
    return "TOPICS: supply and demand\n" + "\n".join(
        " ".join(f"f{i}w{j}" for j in range(60)) for i in range(6)
    )


def test_process_reports_folding_between_the_map_and_reduce_stages():
    """Folding runs model calls, so the client must not go quiet across it."""
    _, handler, items, _ = _run(_transcript(400), budget=2_000, note=_long_note())

    stages = [p["stage"] for p in _progress(items)]
    assert "fold" in stages, "notes this large must force a fold"

    last_map = max(i for i, s in enumerate(stages) if s == "map")
    first_fold = stages.index("fold")
    assert first_fold > last_map, "folding starts once the map stage is done"
    assert stages.index("reduce") > max(i for i, s in enumerate(stages) if s == "fold")

    # No silent gap: every fold call is announced by a fold event.
    fold_calls = [c for c in handler.map_calls
                  if c["messages"][-1]["content"].startswith("[NOTES ")]
    assert len(fold_calls) == stages.count("fold")


def test_every_fold_round_is_announced_in_the_full_stream():
    """A second round restarts the numbering; it must not reuse round 1's."""
    _, handler, items, _ = _run(_transcript(400), budget=2_000, note=_long_note())

    fold_events = [p for p in _progress(items) if p["stage"] == "fold"]
    assert fold_events

    for round_no in sorted({e["round"] for e in fold_events}):
        this_round = [e for e in fold_events if e["round"] == round_no]
        assert [e["chunk"] for e in this_round] == list(range(1, len(this_round) + 1))
        assert all(e["chunks"] == len(this_round) for e in this_round)


def test_fold_progress_in_the_full_stream_precedes_its_own_call():
    """The interleaving rule holds across map and fold alike, in one pass."""
    _, handler, _, _ = _run(_transcript(400), budget=2_000, note=_long_note())

    announced = None
    for kind, payload in handler.timeline:
        if kind == "yield" and isinstance(payload, dict):
            # Warnings announce no call of their own, so they do not take part
            # in the interleaving this asserts.
            if payload.get("event") != "progress":
                continue
            if payload["stage"] == "reduce":
                break
            assert announced is None, (
                f"a {payload['stage']} event was emitted while an earlier one "
                "had no call of its own: progress is running ahead of the work"
            )
            announced = payload
        elif kind == "generate":
            assert announced is not None, "a call ran with nothing announcing it"
            kind_label = "SEGMENT" if announced["stage"] == "map" else "NOTES"
            header = f"[{kind_label} {announced['chunk']}/{announced['chunks']}]"
            assert payload["messages"][-1]["content"].startswith(header), (
                f"the call after progress for {header} processed something else"
            )
            announced = None

    assert announced is None, "the last unit was announced but never processed"


def test_chunking_disabled_falls_back_to_single_shot():
    from utils.config_loader import config

    chunking = config.models.text_gen.chunking
    original = chunking.enabled
    chunking.enabled = False
    try:
        _, handler, items, _ = _run(_transcript(400), budget=2_000)
        assert len(handler.calls) == 1
        assert _progress(items) == []
    finally:
        chunking.enabled = original

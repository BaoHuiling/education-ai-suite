"""Board (content-screen) OCR readers, shared by the HTTP API (``api.board_ocr``)
and the audio-summary pipeline (``summarizer_component``).
"""
import difflib
import json
import os
import logging
import re
from typing import List, Optional, Tuple

from fastapi import HTTPException
from utils.runtime_config_loader import RuntimeConfig
from utils.session_paths import SessionPaths

logger = logging.getLogger(__name__)


def read_board_ocr(session_id: Optional[str]) -> dict:
    """Raw board OCR extraction for a session, falling back to the controller's
    active session when `session_id` is None.

    Returns {session_id, status, count, results[], text}. `status` is one of
    "done", "ocr_in_progress", "frame_extraction_in_progress", "not_started".
    """
    from components.board_ocr.board_ocr_pipeline import (
        get_active_session_id,
        get_status,
    )

    if not session_id:
        session_id = get_active_session_id()

    if not session_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "No board OCR session available. Provide x-session-id header, "
                "or enable board_ocr in config.yaml with a source."
            ),
        )

    status = get_status(session_id)

    if status == "not_started":
        raise HTTPException(
            status_code=404,
            detail=f"No board OCR result found for session {session_id}",
        )

    ocr_path = str(SessionPaths.board_ocr_dir(session_id) / "board_ocr.txt")
    results = []
    if os.path.exists(ocr_path):
        try:
            with open(ocr_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            f"Skipping malformed board OCR line in {ocr_path}"
                        )
        except Exception as e:
            logger.error(f"Error reading board OCR result: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    combined_text = "\n\n".join(r.get("text", "") for r in results if r.get("text"))
    return {
        "session_id": session_id,
        "status": status,
        "count": len(results),
        "results": results,
        "text": combined_text,
    }


def combined_board_text(board: dict) -> str:
    """Board text from a read_board_ocr() result, ready for an LLM prompt:
    deduped, then flattened to one line per frame.

    Dedup runs even when the status is "done": the status flips as soon as the
    frame backlog drains, a moment before the worker's finalize pass rewrites
    the file, so a "done" read can still see raw records. ``clean_records`` is
    idempotent and leaves the file untouched, so this is always safe.
    """
    from components.board_ocr.board_ocr_pipeline import clean_records

    records = clean_records(board.get("results") or [])
    if board.get("status") != "done":
        logger.info(
            f"Board OCR still {board.get('status')} for session {board.get('session_id')}; "
            f"cleaned {board.get('count')} -> {len(records)} records in memory "
            f"(summary may not cover the whole session)"
        )

    slides = []
    for rec in records:
        lines = [ln.strip() for ln in (rec.get("text") or "").splitlines() if ln.strip()]
        if lines:
            slides.append(" ".join(lines))
    return "\n".join(slides)


def read_board_ocr_with_status(session_id: Optional[str]) -> Tuple[str, str]:
    """(board_text, status) for a session; ("", "not_started") if unavailable.

    Non-raising. Callers running alongside the pipeline use the status to warn
    that their board section only covers what has been OCR'd so far.
    """
    try:
        board = read_board_ocr(session_id)
    except HTTPException:
        return "", "not_started"
    return combined_board_text(board), board.get("status", "not_started")


# Near-duplicate lines: OCR re-reads the same board line in every frame, each
# time with slightly different noise. Short lines are left alone ("2", "1. 惊喜")
# since a few characters say too little to call two of them the same line.
_NEAR_DUP_MIN_CHARS = 8
_NEAR_DUP_RATIO = 0.8
# How many recent distinct lines a new one is compared against. The board a
# line repeats on is the one on screen now, not one from the start of the lesson.
_NEAR_DUP_WINDOW = 300
_DIGITS_RE = re.compile(r"\d+")


def _near_duplicate_index(line: str, kept: List[str]) -> Optional[int]:
    """Index of a recent kept line that ``line`` is an OCR variant of, else None.

    Lines whose numbers differ stay distinct: "第1题 ..." and "第2题 ..." match
    on everything else, and are still two exercises.
    """
    digits = _DIGITS_RE.findall(line)
    start = max(0, len(kept) - _NEAR_DUP_WINDOW)
    for idx in range(len(kept) - 1, start - 1, -1):
        other = kept[idx]
        if len(other) < _NEAR_DUP_MIN_CHARS:
            continue
        if abs(len(line) - len(other)) > 0.3 * max(len(line), len(other)):
            continue
        if _DIGITS_RE.findall(other) != digits:
            continue
        matcher = difflib.SequenceMatcher(None, line, other, autojunk=False)
        if (matcher.real_quick_ratio() >= _NEAR_DUP_RATIO
                and matcher.quick_ratio() >= _NEAR_DUP_RATIO
                and matcher.ratio() >= _NEAR_DUP_RATIO):
            return idx
    return None


def distinct_board_lines(board: dict) -> List[str]:
    """The distinct text lines of a read_board_ocr() result, in the order they
    first appeared.

    Each frame repeats most of what is already on the board, so one line per
    frame (combined_board_text) carries the same text many times over. Here a
    line is kept once; of its OCR variants the longest is kept, in the place
    the first one appeared.
    """
    from components.board_ocr.board_ocr_pipeline import clean_records

    kept: List[str] = []
    seen = set()
    for rec in clean_records(board.get("results") or []):
        for raw in (rec.get("text") or "").splitlines():
            line = " ".join(raw.split())
            if not line or line in seen:
                continue
            seen.add(line)
            if len(line) >= _NEAR_DUP_MIN_CHARS:
                idx = _near_duplicate_index(line, kept)
                if idx is not None:
                    if len(line) > len(kept[idx]):
                        kept[idx] = line
                    continue
            kept.append(line)
    return kept


def read_board_ocr_lines_with_status(session_id: Optional[str]) -> Tuple[List[str], str]:
    """(distinct board lines, status) for a session; ([], "not_started") if unavailable.

    Non-raising, like read_board_ocr_with_status.
    """
    try:
        board = read_board_ocr(session_id)
    except HTTPException:
        return [], "not_started"
    return distinct_board_lines(board), board.get("status", "not_started")


def board_summary_system_prompt(lang: str) -> str:
    """Standalone system prompt for summarizing board/screen OCR text.

    Distinct from ``prompts/summarizer/<lang>/board_ocr_addendum.txt`` (which is
    phrased as an addendum to the audio-transcript summary); this one stands on
    its own: the /board-ocr/summary endpoint uses it, and so does the summarizer
    when it has to condense a board too large for the lesson summary call.
    """
    if lang == "zh":
        return (
            "你会收到一段板书内容：通过 OCR 从教室显示屏/交互式白板逐帧捕获的文本"
            "（幻灯片标题、要点、表格、公式等），按时间先后排列，可能含有 OCR 噪声、"
            "水印或网站/频道名称，且同一标题可能在多帧中重复出现。\n\n"
            "请综合这些内容，直接输出摘要正文，组织为若干条要点。\n\n"
            "规则:\n"
            "- 用完整、通顺的句子说明板书/屏幕上“呈现了/讲解了/说明了/描述了/列举了”哪些内容，"
            "而不是罗列零散的关键词或短语。\n"
            "- 将同一主题的重复帧或相关帧归纳为一句连贯的表述。\n"
            "- 按主题或授课顺序组织为若干条要点，每条都是一个完整句子。\n"
            "- 忽略水印、网站/频道名称等无关噪声；在含义明确时修正明显的 OCR 错误。\n"
            "- 不要输出任何标题或章节名，直接给出要点。\n"
            "- 如果板书内容为空或无法识别，输出“无”。"
        )
    return (
        "You will receive board content: text captured frame by frame by OCR from a "
        "classroom display / interactive flat panel (slide titles, bullet points, tables, "
        "equations), in chronological order. It may contain OCR noise, watermarks, or "
        "site/channel names, and the same title may repeat across many frames.\n\n"
        "Synthesize this content and output the summary body directly as a few bullet "
        "points.\n\n"
        "Rules:\n"
        "- Write complete, fluent sentences describing WHAT the board presented, e.g. "
        "\"Showed ...\", \"Explained ...\", \"Described ...\", \"Listed ...\".\n"
        "- Do NOT output isolated keywords or fragments. Merge repeated/related frames on "
        "the same topic into one coherent statement.\n"
        "- Organize into a few bullet points by topic or teaching sequence; each bullet is a "
        "full sentence.\n"
        "- Ignore watermarks, channel/site names, and other noise; fix obvious OCR errors "
        "when the meaning is clear.\n"
        "- Do NOT output any heading or section title; give the bullet points directly.\n"
        "- If the board content is empty or unreadable, output \"None\"."
    )

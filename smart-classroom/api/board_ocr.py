# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import logging
from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from components.board_ocr.board_ocr_service import (
    board_summary_system_prompt,
    combined_board_text,
    read_board_ocr,
)
from utils.config_loader import config

logger = logging.getLogger(__name__)

board_ocr_router = APIRouter()


def summarize_board_ocr(session_id: Optional[str]) -> dict:
    """Summarize the board OCR text via the text_gen (VLM) capability."""
    from model_manager import ModelManager

    board = read_board_ocr(session_id)
    board_text = combined_board_text(board)

    if not board_text:
        logger.info(
            f"Board OCR summary requested for session {board['session_id']} — "
            f"no board text available, returning empty summary"
        )
        return {
            "session_id": board["session_id"],
            "status": "no_board_text",
            "board_ocr_status": board["status"],
            "frames": board["count"],
            "board_text_chars": 0,
            "summary": None,
        }

    tg = ModelManager.instance().text_gen()

    messages = [
        {"role": "system", "content": board_summary_system_prompt(config.app.language)},
        {"role": "user", "content": board_text},
    ]
    raw = tg.generate(messages=messages, stream=False, enable_thinking=False)
    summary = raw if isinstance(raw, str) else "".join(raw)

    logger.info(
        f"Board OCR summary generated for session {board['session_id']} "
        f"({board['count']} frames, {len(board_text)} chars -> {len(summary)} chars)"
    )

    return {
        "session_id": board["session_id"],
        "status": "done",
        "board_ocr_status": board["status"],
        "frames": board["count"],
        "board_text_chars": len(board_text),
        "summary": summary,
    }


@board_ocr_router.get("/board-ocr/ocr")
def get_board_ocr_endpoint(x_session_id: Optional[str] = Header(None)):
    """Return the board (content-screen) OCR extraction + status."""
    return JSONResponse(content=read_board_ocr(x_session_id), status_code=200)


@board_ocr_router.post("/board-ocr/summary")
def board_ocr_summary_endpoint(x_session_id: Optional[str] = Header(None)):
    """Summarize the board OCR text via the text_gen (VLM) capability."""
    return JSONResponse(content=summarize_board_ocr(x_session_id), status_code=200)

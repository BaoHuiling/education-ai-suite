# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Board OCR reaches the summary as its distinct lines, not frame by frame."""

import os
import sys
from unittest.mock import patch

_SC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)

from components.board_ocr.board_ocr_service import distinct_board_lines


def _lines(*frames):
    """distinct_board_lines over frames given as lists of lines; record-level
    cleaning is left out so each test sees only the line dedup."""
    board = {"results": [{"text": "\n".join(f)} for f in frames]}
    with patch("components.board_ocr.board_ocr_pipeline.clean_records",
               side_effect=lambda records: records):
        return distinct_board_lines(board)


def test_a_line_repeated_across_frames_is_kept_once_in_first_seen_order():
    assert _lines(
        ["Lesson 3: fractions", "Add the numerators"],
        ["Lesson 3: fractions", "Add the numerators", "Keep the denominator"],
    ) == ["Lesson 3: fractions", "Add the numerators", "Keep the denominator"]


def test_whitespace_differences_do_not_make_a_line_new():
    assert _lines(["Add  the numerators "], ["Add the numerators"]) == ["Add the numerators"]


def test_ocr_variants_of_one_line_collapse_to_the_longest():
    assert _lines(
        ["Keep the denominat"],
        ["Keep the denominator"],
        ["Keep the denomlnator"],
    ) == ["Keep the denominator"]


def test_lines_that_differ_in_their_numbers_stay_distinct():
    """Numbered exercises match on everything but the number."""
    assert _lines(["Exercise 1: simplify the fraction"],
                  ["Exercise 2: simplify the fraction"]) == [
        "Exercise 1: simplify the fraction",
        "Exercise 2: simplify the fraction",
    ]


def test_short_lines_are_only_dropped_when_identical():
    assert _lines(["1. ab", "2. ab", "1. ab"]) == ["1. ab", "2. ab"]


def test_unrelated_lines_are_all_kept():
    assert _lines(["Photosynthesis needs light"], ["Mitochondria make energy"]) == [
        "Photosynthesis needs light",
        "Mitochondria make energy",
    ]


def test_an_empty_board_gives_no_lines():
    assert _lines() == []
    assert _lines([""], ["   "]) == []

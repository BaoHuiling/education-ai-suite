# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""student_count is robust to momentary missed and doubled detections.

It used to be the mean of the person counts at frames 900/1800/2700 -- three
snapshots from the first three minutes at 15 fps -- so any one frame with
occluded students dragged it down. Each minute now takes a percentile of its
per-frame counts (the median by default) and the class value is the median
over minutes.

The service is built with object.__new__, as in test_va_pipeline_stop.py, and
reads a real front_posture.txt from a temporary directory.
"""

import json
import logging

from components.va.va_pipeline_service import VideoAnalyticsPipelineService

S = 1_000_000_000  # ns per second
FPS = 15


def make_service():
    svc = object.__new__(VideoAnalyticsPipelineService)
    svc.logger = logging.getLogger("test.va.count")
    svc.max_input_framerate = "15/1"
    svc.count_bucket_sec = 60
    svc.count_percentile = 50
    svc.raise_confirm_sec = 0.13
    svc.raise_confirm_sec_unid = 0.93
    svc.stand_confirm_sec = 0.6
    svc.absence_sec = 6.0
    svc.stale_unidentified_sec = 2.0
    svc.center_dist_threshold = 0.1
    svc.unidentified_max = 50
    return svc


def person(i):
    x = (i % 20) * 0.05
    return {"detection": {"bounding_box": {"x_min": x, "x_max": x + 0.04,
                                           "y_min": 0.5, "y_max": 0.6},
                          "label": "sit"}}


def write(path, counts, start_sec=0.0, timestamps=True):
    """Append one frame per entry in counts, FPS frames per second."""
    with open(path, "a", encoding="utf-8") as fh:
        for k, n in enumerate(counts):
            frame = {"objects": [person(i) for i in range(n)]}
            if timestamps:
                frame["timestamp"] = round((start_sec + k / FPS) * S)
            fh.write(json.dumps(frame) + "\n")


def flicker(n_seconds, present, missed_every=3, missed=8):
    """`present` students; every `missed_every`-th frame loses `missed` of them."""
    return [present - missed if k % missed_every == 0 else present
            for k in range(n_seconds * FPS)]


def test_transient_misses_do_not_lower_count(tmp_path):
    posture = tmp_path / "front_posture.txt"
    write(posture, flicker(300, present=40))
    stats, _ = make_service().get_pose_stats(str(posture))
    # The mean of these frames would be ~37.
    assert stats["student_count"] == 40


def test_transient_double_detections_do_not_raise_count(tmp_path):
    posture = tmp_path / "front_posture.txt"
    write(posture, [c + 6 if k % 4 == 0 else c for k, c in enumerate([30] * (300 * FPS))])
    stats, _ = make_service().get_pose_stats(str(posture))
    assert stats["student_count"] == 30


def test_count_follows_whole_class_not_first_minutes(tmp_path):
    posture = tmp_path / "front_posture.txt"
    # Students still arriving for 3 minutes, then a full class for 10.
    write(posture, [10] * (180 * FPS) + [35] * (600 * FPS))
    stats, _ = make_service().get_pose_stats(str(posture))
    assert stats["student_count"] == 35


def test_incremental_matches_one_shot(tmp_path):
    counts = flicker(200, present=25) + flicker(200, present=30)
    whole = tmp_path / "whole.txt"
    write(whole, counts)
    one_shot, _ = make_service().get_pose_stats(str(whole))

    svc = make_service()
    parts = tmp_path / "parts.txt"
    state = None
    step = 7 * FPS
    for start in range(0, len(counts), step):
        write(parts, counts[start:start + step], start_sec=start / FPS)
        stats, state = svc.get_pose_stats(str(parts), state)
    assert stats == one_shot


def test_short_clip_uses_partial_bucket(tmp_path):
    posture = tmp_path / "front_posture.txt"
    write(posture, flicker(20, present=12, missed=4))
    stats, _ = make_service().get_pose_stats(str(posture))
    assert stats["student_count"] == 12


def test_missing_timestamps_fall_back_to_frame_index(tmp_path):
    posture = tmp_path / "front_posture.txt"
    write(posture, [5] * (120 * FPS) + [20] * (240 * FPS), timestamps=False)
    stats, state = make_service().get_pose_stats(str(posture))
    assert stats["student_count"] == 20
    assert len(state["count_bucket_values"]) == 5  # 6 minutes, last one still open

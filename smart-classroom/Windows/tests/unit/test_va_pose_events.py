# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Stand / raise-hand events do not depend on the pose inference rate.

The pose model may be inferred on every frame or every Nth one (gvadetect
inference-interval, with gvatrack carrying the boxes in between). Statistics
read only the inferred frames -- gvaposturedetect marks the others label_id 0
-- and the confirmation windows are in seconds on frame timestamps, so the
same scene yields the same events at 15 fps and at 5 fps.

The service is built with object.__new__, as in test_va_pipeline_stop.py, and
reads a real front_posture.txt from a temporary directory.
"""

import json
import logging

import pytest

from components.va.va_pipeline_service import VideoAnalyticsPipelineService

S = 1_000_000_000  # ns per second


def make_service():
    svc = object.__new__(VideoAnalyticsPipelineService)
    svc.logger = logging.getLogger("test.va.events")
    svc.max_input_framerate = "15/1"
    svc.count_bucket_sec = 60
    svc.count_percentile = 90
    svc.raise_confirm_sec = 0.4
    svc.raise_confirm_sec_unid = 0.8
    svc.stand_confirm_sec = 0.6
    svc.absence_sec = 6.0
    svc.stale_unidentified_sec = 2.0
    svc.center_dist_threshold = 0.1
    svc.unidentified_max = 50
    return svc


def obj(x, label, student_id=0):
    o = {"detection": {"bounding_box": {"x_min": x, "x_max": x + 0.04,
                                        "y_min": 0.5, "y_max": 0.6},
                       "label": label}}
    if student_id:
        o["id"] = student_id
    return o


def scene(t):
    """One seated student (x=0.1) raising a hand during 3-5 s, and a student
    (x=0.6, ReID id 7) standing during 10-14 s."""
    objs = [obj(0.1, "sit_raise_up" if 3 <= t < 5 else "sit")]
    objs.append(obj(0.6, "stand", 7) if 10 <= t < 14 else obj(0.6, "sit"))
    return objs


def write(path, fps, interval=1, seconds=20):
    """Frames at `fps`; with interval > 1 only every interval-th one is
    inferred and the rest are marked tracked (holding the last inference)."""
    with open(path, "w", encoding="utf-8") as fh:
        held = []
        for k in range(int(seconds * fps)):
            t = k / fps
            inferred = k % interval == 0
            if inferred:
                held = scene(t)
            marker = {"x": 0, "y": 0, "w": 1920, "h": 1080,
                      "posture_frame": {"label_id": 1 if inferred else 0}}
            fh.write(json.dumps({"objects": held + [marker], "timestamp": round(t * S)}) + "\n")


@pytest.mark.parametrize("interval", [1, 2, 3, 5])
def test_events_independent_of_inference_interval(tmp_path, interval):
    posture = tmp_path / "front_posture.txt"
    write(posture, fps=15, interval=interval)
    stats, _ = make_service().get_pose_stats(str(posture))
    assert stats["raise_up_count"] == 1
    assert stats["stand_count"] == 1
    assert stats["stand_reid"] == [{"student_id": 7, "count": 1}]


def test_tracked_frames_are_not_counted(tmp_path):
    posture = tmp_path / "front_posture.txt"
    with open(posture, "w", encoding="utf-8") as fh:
        for k in range(30):
            # Tracked frames flipping the label must not create transitions.
            label = "sit_raise_up" if k % 3 else "sit"
            marker = {"posture_frame": {"label_id": 0 if k % 3 else 1}}
            fh.write(json.dumps({"objects": [obj(0.1, label), marker],
                                 "timestamp": round(k / 15 * S)}) + "\n")
    stats, _ = make_service().get_pose_stats(str(posture))
    assert stats["raise_up_count"] == 0


def test_brief_raise_flicker_is_filtered(tmp_path):
    posture = tmp_path / "front_posture.txt"
    with open(posture, "w", encoding="utf-8") as fh:
        for k in range(15 * 10):
            t = k / 15
            # 0.2 s blips, shorter than raise_confirm_sec_unid
            label = "sit_raise_up" if int(t * 10) % 20 in (0, 1) and t > 1 else "sit"
            fh.write(json.dumps({"objects": [obj(0.1, label)], "timestamp": round(t * S)}) + "\n")
    stats, _ = make_service().get_pose_stats(str(posture))
    assert stats["raise_up_count"] == 0


def test_timestamp_restart_resets_tracking(tmp_path):
    posture = tmp_path / "front_posture.txt"
    svc = make_service()
    write(posture, fps=15, seconds=12)  # stand from 10 s, confirmed by 10.6 s
    # Restarted pipeline: timestamps start again at 0 with the student standing.
    with open(posture, "a", encoding="utf-8") as fh:
        for k in range(15):
            fh.write(json.dumps({"objects": [obj(0.6, "stand", 7)],
                                 "timestamp": round(k / 15 * S)}) + "\n")
    stats, _ = svc.get_pose_stats(str(posture))
    assert stats["stand_count"] == 2

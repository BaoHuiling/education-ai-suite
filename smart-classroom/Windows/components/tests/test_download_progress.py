# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Model downloads report byte-level progress to the desktop app, and only to it."""

import importlib
import json
import logging

from pathlib import Path
from types import SimpleNamespace

import huggingface_hub
import pytest
from huggingface_hub import file_download
from huggingface_hub.utils import _xet
from huggingface_hub.utils.tqdm import _create_progress_bar
from tqdm.contrib.concurrent import thread_map

from utils import download_progress as dp
from utils import model_download_helper as helper

ms_download = importlib.import_module("modelscope.hub.snapshot_download")


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _progress(clock=None, interval=0.5):
    lines = []
    progress = dp.DownloadProgress("org/model", "hf", emit=lines.append,
                                   clock=clock or Clock(), interval=interval)
    return progress, lines


def _events(lines):
    assert all(line.startswith(dp.PREFIX) for line in lines)
    return [json.loads(line[len(dp.PREFIX):]) for line in lines]


def test_silent_outside_the_app(monkeypatch):
    monkeypatch.delenv(dp.ENV_VAR, raising=False)
    assert dp.track("org/model", "hf") is None
    assert dp.hf_kwargs(None, huggingface_hub.snapshot_download) == {}
    with dp.reporting(None) as progress:
        assert progress is None


def test_hugging_face_bars_feed_bytes_and_files():
    progress, lines = _progress(interval=0)
    cls = progress.hf_tqdm_class()

    # As snapshot_download makes its bar over all files' bytes. A class that is
    # not Hugging Face's own gets neither `disable` nor `name`, so it stays on
    # when output is not a terminal.
    bar = _create_progress_bar(cls=cls, log_level=logging.WARNING, name="huggingface_hub.snapshot_download",
                               desc="Downloading (incomplete total...)", total=0, initial=0, unit="B",
                               unit_scale=True)
    bar.total += 1000
    bar.refresh()
    bar.update(400)
    bar.total += 500
    bar.refresh()
    bar.update(1100)
    # And its count of files, through tqdm's thread_map, locks and all.
    thread_map(lambda item: item, range(3), desc="Fetching 3 files", tqdm_class=cls, max_workers=2)

    events = _events(lines)
    # The total grows as files start, and the app sees each step.
    assert any(e["done"] == 400 and e["total"] == 1000 for e in events)
    last = events[-1]
    assert (last["done"], last["total"], last["files"], last["fileTotal"]) == (1500, 1500, 3, 3)
    assert last["hub"] == "huggingface" and last["repo"] == "org/model"


def test_modelscope_retries_do_not_count_twice():
    progress, lines = _progress(interval=0)
    cls = progress.ms_callback_class()

    first = cls("model.safetensors", 1000)
    first.update(600)
    # A retry reports again what the partial file already holds.
    first.update(600)
    first.update(400)
    first.end()
    small = cls("config.json", 0)  # size unknown: counted as it comes
    small.update(50)
    small.end()

    last = _events(lines)[-1]
    assert (last["done"], last["total"], last["files"], last["fileTotal"]) == (1050, 1050, 2, None)


def test_lines_are_throttled_but_start_and_end_always_go_out():
    clock = Clock()
    progress, lines = _progress(clock=clock)
    with progress:
        for _ in range(10):
            progress.advance(10)  # all within the same instant
        clock.now += 0.6
        progress.advance(10)
    states = [event["state"] for event in _events(lines)]
    assert states == ["running", "running", "done"]


def test_speed_is_measured_over_the_last_seconds():
    clock = Clock()
    progress, lines = _progress(clock=clock, interval=0)
    with progress:
        progress.add_file(10_000)
        assert _events(lines)[-1]["speed"] is None
        for _ in range(4):
            clock.now += 1.0
            progress.advance(1000)
    running = [event for event in _events(lines) if event["state"] == "running"]
    assert running[-1]["speed"] == pytest.approx(1000.0)


def test_a_failed_download_says_so_and_the_error_goes_on():
    progress, lines = _progress()
    with pytest.raises(RuntimeError):
        with progress:
            progress.advance(10)
            raise RuntimeError("network down")
    assert _events(lines)[-1]["state"] == "failed"


def test_download_snapshot_reports_through_both_hubs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(dp.ENV_VAR, "1")
    monkeypatch.setattr(helper, "warn_if_not_configured_hub", lambda *a: None)

    def hf_fake(repo_id, revision=None, local_dir=None, allow_patterns=None, token=None, tqdm_class=None):
        bar = tqdm_class(total=0, unit="B")
        bar.total += 2048
        bar.refresh()
        bar.update(2048)
        return local_dir

    def ms_fake(repo_id, revision=None, local_dir=None, allow_patterns=None, progress_callbacks=None):
        for callback in progress_callbacks:
            file = callback("weights.bin", 4096)
            file.update(4096)
            file.end()
        return local_dir

    monkeypatch.setattr(huggingface_hub, "snapshot_download", hf_fake)
    monkeypatch.setattr(ms_download, "snapshot_download", ms_fake)

    helper.download_snapshot("hf", "org/a", str(tmp_path / "a"))
    helper.download_snapshot("ms", "org/b", str(tmp_path / "b"))

    events = _events([line for line in capsys.readouterr().out.splitlines() if line.startswith(dp.PREFIX)])
    ends = [event for event in events if event["state"] == "done"]
    assert [(e["repo"], e["hub"], e["done"]) for e in ends] == [
        ("org/a", "huggingface", 2048), ("org/b", "modelscope", 4096)]


def test_a_hub_without_the_hook_is_called_as_before(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(dp.ENV_VAR, "1")
    monkeypatch.setattr(helper, "warn_if_not_configured_hub", lambda *a: None)
    seen = {}

    def old_hf(repo_id, revision=None, local_dir=None, allow_patterns=None, token=None):
        seen["called"] = True
        return local_dir

    monkeypatch.setattr(huggingface_hub, "snapshot_download", old_hf)
    helper.download_snapshot("hf", "org/a", str(tmp_path / "a"))
    assert seen == {"called": True}
    # Start and end still go out, so the app knows which model it was.
    states = [e["state"] for e in _events([l for l in capsys.readouterr().out.splitlines() if l.startswith(dp.PREFIX)])]
    assert states == ["running", "done"]


class XetSession:
    """Stands in for hf_xet's session: each download reports as `reports` say."""

    def __init__(self, reports, fail=False):
        self.reports = reports
        self.fail = fail

    def new_file_download_group(self, *, progress_callback=None, **_):
        session = self

        class Group:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def start_download_file(self, info, path):
                for report in session.reports:
                    progress_callback(report, {})
                if session.fail:
                    raise ConnectionError("connection reset")

        return Group()


def _xet_report(counted, to_receive, received, size=1000):
    return SimpleNamespace(total_bytes=size, total_bytes_completed=counted,
                           total_transfer_bytes=to_receive, total_transfer_bytes_completed=received)


def _xet_download(progress, session, monkeypatch, tmp_path):
    """Hugging Face's own xet_get, against a stand-in session."""
    monkeypatch.setattr(_xet, "get_xet_session", lambda: session)
    bar = _create_progress_bar(cls=progress.hf_tqdm_class(), log_level=logging.WARNING, name="test",
                               desc="weights", total=1000, initial=0, unit="B", unit_scale=True)
    with progress:
        file_download.xet_get(incomplete_path=Path(tmp_path / "weights.incomplete"),
                              xet_file_data=SimpleNamespace(file_hash="abc", refresh_route="/refresh"),
                              headers={}, expected_size=1000, _tqdm_bar=bar)


def test_xet_files_move_while_they_download(monkeypatch, tmp_path):
    # hf_xet counts the file only once it is whole, but the bytes received
    # (800 compressed for 1000) show how far it is.
    progress, lines = _progress(interval=0)
    session = XetSession([_xet_report(0, 0, 0), _xet_report(0, 800, 200), _xet_report(0, 800, 600),
                          _xet_report(1000, 800, 800)])
    _xet_download(progress, session, monkeypatch, tmp_path)
    done = [event["done"] for event in _events(lines)]
    assert 250 in done and 750 in done
    assert done == sorted(done)
    assert _events(lines)[-1]["done"] == 1000


def test_a_failed_xet_file_gives_back_its_share(monkeypatch, tmp_path):
    progress, lines = _progress(interval=0)
    with pytest.raises(ConnectionError):
        _xet_download(progress, XetSession([_xet_report(0, 800, 400)], fail=True), monkeypatch, tmp_path)
    events = _events(lines)
    assert any(event["done"] == 500 for event in events)
    assert (events[-1]["state"], events[-1]["done"]) == ("failed", 0)


def test_a_xet_report_without_network_counts_changes_nothing(monkeypatch, tmp_path):
    # As from an hf_xet that no longer counts what it received.
    progress, lines = _progress(interval=0)
    reports = [SimpleNamespace(total_bytes_completed=0), SimpleNamespace(total_bytes_completed=1000)]
    _xet_download(progress, XetSession(reports), monkeypatch, tmp_path)
    assert [event["done"] for event in _events(lines)][-2:] == [1000, 1000]


def test_xet_downloads_are_left_alone_when_nothing_follows_them(monkeypatch, tmp_path):
    progress, lines = _progress(interval=0)
    with progress:
        pass
    calls = []
    monkeypatch.setattr(_xet, "get_xet_session", lambda: XetSession([_xet_report(0, 800, 400)]))
    with _create_progress_bar(cls=progress.hf_tqdm_class(), log_level=logging.WARNING, name="test",
                              desc="weights", total=1000, initial=0, unit="B") as bar:
        bar.update = lambda n=1: calls.append(n)
        file_download.xet_get(incomplete_path=Path(tmp_path / "w.incomplete"),
                              xet_file_data=SimpleNamespace(file_hash="abc", refresh_route="/r"),
                              headers={}, expected_size=1000, _tqdm_bar=bar)
    assert calls == [0]
    assert progress.done == 0

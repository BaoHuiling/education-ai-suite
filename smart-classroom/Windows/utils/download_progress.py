# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Byte-level progress of model downloads, for the desktop app.

The desktop app starts the backend and its setup scripts with
``SC_PROGRESS_EVENTS=1`` and reads their output. While a model downloads,
:class:`DownloadProgress` prints one line when it starts, at most one every
half second while it runs, and one when it ends::

    @@download {"repo":"Qwen/Qwen3-8B","hub":"huggingface","state":"running",
                "done":1234,"total":5678,"speed":901.2,"files":3,"fileTotal":12}

The app shows that as a progress bar instead of a log line
(electron/services/download-progress.cjs). Without the variable nothing is
printed and nothing is hooked into the hubs: a terminal keeps the hubs' own
progress bars.

``done`` and ``total`` are bytes; ``speed`` is bytes a second over the last
few seconds. ``total`` can still grow early in a download: a hub learns a
file's size only when that file starts, and it starts a few at a time.
``fileTotal`` is null when the hub does not say how many files there are.
"""

import contextlib
import functools
import inspect
import json
import logging
import os
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PREFIX = "@@download "
ENV_VAR = "SC_PROGRESS_EVENTS"
INTERVAL = 0.5
SPEED_WINDOW = 5.0
# The app's names for the hubs (ModelHub in ui/src/types/firstRun.ts).
HUB_NAMES = {"hf": "huggingface", "ms": "modelscope"}

_stdout_lock = threading.Lock()


def enabled() -> bool:
    """Whether the app is reading progress lines."""
    return os.environ.get(ENV_VAR) == "1"


def _print_line(line: str) -> None:
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def accepts(function: Callable, parameter: str) -> bool:
    """Whether ``function`` declares ``parameter``: older hub versions lack the hooks."""
    try:
        return parameter in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


class DownloadProgress:
    """Bytes downloaded for one snapshot, reported from any number of threads.

    Use as a context manager around the download, which reports the start and
    whether it ended in success. The hubs feed it through
    :meth:`hf_tqdm_class` and :meth:`ms_callback_class`.
    """

    def __init__(self, repo: str, hub: str, *, emit: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.monotonic, interval: float = INTERVAL):
        self.repo = repo
        self.hub = HUB_NAMES.get(hub, hub)
        self.done = 0
        self.total = 0
        self.files = 0
        self.file_total: Optional[int] = None
        self.state = "running"
        self._emit = emit if emit is not None else _print_line
        self._clock = clock
        self._interval = interval
        self._lock = threading.Lock()
        self._started = clock()
        self._last_line: Optional[float] = None
        self._samples = []  # (time, done), the last SPEED_WINDOW seconds
        self._counted = 0  # bytes on Hugging Face's bar
        self._ahead = {}  # Xet file -> bytes received but not on the bar yet
        self._quiet = 0  # inside as_one(): lines wait for the end

    # -- reporting ---------------------------------------------------------

    def speed(self, now: Optional[float] = None) -> Optional[float]:
        """Bytes a second over the last few seconds; None until there is a span to measure."""
        now = self._clock() if now is None else now
        if len(self._samples) < 2:
            return None
        start_time, start_done = self._samples[0]
        span = now - start_time
        return (self.done - start_done) / span if span >= 1.0 else None

    def line(self, now: Optional[float] = None) -> str:
        speed = self.speed(now)
        return PREFIX + json.dumps({
            "repo": self.repo,
            "hub": self.hub,
            "state": self.state,
            "done": self.done,
            "total": max(self.total, self.done),
            "speed": round(speed, 1) if speed is not None else None,
            "files": self.files,
            "fileTotal": self.file_total,
        }, separators=(",", ":"), ensure_ascii=False)

    def _changed(self, force: bool = False) -> None:
        """Called with the lock held, after any change."""
        if self._quiet and not force:
            return
        now = self._clock()
        self._samples.append((now, self.done))
        while len(self._samples) > 2 and now - self._samples[1][0] >= SPEED_WINDOW:
            self._samples.pop(0)
        if force or self._last_line is None or now - self._last_line >= self._interval:
            self._last_line = now
            self._emit(self.line(now))

    @contextlib.contextmanager
    def as_one(self):
        """Changes made inside go out together, at the end.

        A Xet report moves a file's bytes from ahead of Hugging Face's count
        onto it, in two changes; a line between them would show those bytes
        twice, or not at all.
        """
        with self._lock:
            self._quiet += 1
        try:
            yield
        finally:
            with self._lock:
                self._quiet -= 1
                self._changed()

    # -- what the hubs report ------------------------------------------------

    def set_bytes(self, done: int, total: int) -> None:
        """Absolute counts, from Hugging Face's bar over all files."""
        with self._lock:
            self._counted = max(0, int(done or 0))
            self.total = max(0, int(total or 0))
            self.done = self._counted + sum(self._ahead.values())
            self._changed()

    def set_ahead(self, key, size: int) -> None:
        """Bytes of a Xet file received so far but not yet on Hugging Face's bar."""
        with self._lock:
            if size > 0:
                self._ahead[key] = int(size)
            elif self._ahead.pop(key, None) is None:
                return
            self.done = self._counted + sum(self._ahead.values())
            self._changed()

    def xet_report(self, key, report) -> None:
        """A report from hf_xet on one file.

        It counts the file's bytes as done only once the whole file is put
        together, but it also counts the bytes received over the network,
        which are compressed: the share received, of the file's size, is
        what is ahead of the count.
        """
        size = getattr(report, "total_bytes", 0) or 0
        counted = getattr(report, "total_bytes_completed", 0) or 0
        to_receive = getattr(report, "total_transfer_bytes", 0) or 0
        received = getattr(report, "total_transfer_bytes_completed", 0) or 0
        if size and to_receive:
            share = int(size * min(received, to_receive) / to_receive)
            self.set_ahead(key, share - counted)

    def set_files(self, done: int, total: Optional[int]) -> None:
        with self._lock:
            self.files = int(done or 0)
            self.file_total = int(total) if total else None
            self._changed()

    def add_file(self, size: int) -> None:
        """A file starts: ModelScope, which counts per file."""
        with self._lock:
            self.total += max(0, int(size or 0))
            self._changed()

    def advance(self, size: int) -> None:
        with self._lock:
            self.done += max(0, int(size or 0))
            self._changed()

    def file_finished(self) -> None:
        with self._lock:
            self.files += 1
            self._changed()

    # -- start and end ---------------------------------------------------------

    def __enter__(self) -> "DownloadProgress":
        if self.hub == HUB_NAMES["hf"]:
            _follow_xet(self)
        with self._lock:
            self._changed(force=True)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        _unfollow_xet(self)
        self.finish(ok=exc_type is None)
        return False

    def finish(self, ok: bool = True) -> None:
        with self._lock:
            self.state = "done" if ok else "failed"
            if ok:
                self.total = max(self.total, self.done)
            self._changed(force=True)
        if ok and self.done:
            seconds = max(self._clock() - self._started, 0.001)
            logger.info("Downloaded %s: %.1f MB in %.0f s", self.repo, self.done / 2**20, seconds)

    # -- hub adapters ----------------------------------------------------------

    def hf_tqdm_class(self):
        """For ``huggingface_hub.snapshot_download(tqdm_class=...)``.

        Hugging Face makes two bars with it: one over all files' bytes
        (``unit="B"``) and one counting files. This one draws nothing and
        passes both on. It derives from plain tqdm rather than Hugging Face's
        subclass, which would turn itself off when output is not a terminal.
        """
        from tqdm.std import tqdm

        progress = self

        class _Null:
            def write(self, _text):
                pass

            def flush(self):
                pass

        class ProgressTqdm(tqdm):
            def __init__(self, *args, **kwargs):
                kwargs["file"] = _Null()
                kwargs.pop("disable", None)
                kwargs.pop("name", None)
                super().__init__(*args, **kwargs)

            def _report(self):
                if self.unit == "B":
                    progress.set_bytes(self.n, self.total or 0)
                else:
                    progress.set_files(self.n, self.total)

            def update(self, n=1):
                shown = super().update(n)
                self._report()
                return shown

            def refresh(self, *args, **kwargs):
                super().refresh(*args, **kwargs)
                self._report()

        return ProgressTqdm

    def ms_callback_class(self):
        """For ``modelscope snapshot_download(progress_callbacks=[...])``.

        ModelScope makes one per file. When it retries, it reports again the
        bytes already in the partial file, so each file's count stops at the
        file's size rather than running past it.
        """
        from modelscope.hub.callback import ProgressCallback

        progress = self

        class ProgressReport(ProgressCallback):
            def __init__(self, filename: str, file_size: int):
                super().__init__(filename, file_size)
                self._done = 0
                # A large file comes in parts, on several threads at once.
                self._file_lock = threading.Lock()
                progress.add_file(file_size)

            def update(self, size: int) -> None:
                with self._file_lock:
                    if self.file_size and self.file_size > 0:
                        size = max(0, min(size, self.file_size - self._done))
                    self._done += size
                progress.advance(size)

            def end(self) -> None:
                with self._file_lock:
                    rest = max(0, (self.file_size or 0) - self._done)
                    self._done += rest
                if rest:
                    progress.advance(rest)
                progress.file_finished()

        return ProgressReport


# -- Xet ------------------------------------------------------------------------
#
# Hugging Face keeps most large files in Xet storage, and hf_xet counts a
# file's bytes as done only once the whole file is put together. Fed only
# what Hugging Face passes to its bar, the bar would stand still through the
# biggest files and jump at the end. So while a download is followed, the
# Xet session's reports go to it as well (DownloadProgress.xet_report).

_xet_lock = threading.Lock()
_xet_followers = []  # downloads under way, the latest last


def _follow_xet(progress: DownloadProgress) -> None:
    with _xet_lock:
        _xet_followers.append(progress)
        _wrap_xet_session()


def _unfollow_xet(progress: DownloadProgress) -> None:
    with _xet_lock:
        if progress in _xet_followers:
            _xet_followers.remove(progress)


def _xet_follower() -> Optional[DownloadProgress]:
    with _xet_lock:
        return _xet_followers[-1] if _xet_followers else None


def _wrap_xet_session() -> None:
    """Wrap huggingface_hub's Xet session factory, once; called with the lock held.

    xet_get() imports get_xet_session from huggingface_hub.utils._xet each
    time it runs, so the wrapper is what it gets. Where that is not so, or
    the session works differently, downloads go on as before, reported only
    by what Hugging Face passes to its bar.
    """
    try:
        from huggingface_hub.utils import _xet
    except ImportError:
        return
    real = getattr(_xet, "get_xet_session", None)
    if real is None or getattr(real, "_follows_progress", False):
        return

    @functools.wraps(real)
    def get_xet_session(*args, **kwargs):
        return _XetSession(real(*args, **kwargs))

    get_xet_session._follows_progress = True
    _xet.get_xet_session = get_xet_session


class _XetSession:
    """A Xet session whose download reports also reach the download being followed."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def new_file_download_group(self, *args, progress_callback=None, **kwargs):
        progress = _xet_follower()
        if progress is None or progress_callback is None:
            return self._session.new_file_download_group(*args, progress_callback=progress_callback, **kwargs)
        key = object()

        def callback(report, items):
            with progress.as_one():
                result = progress_callback(report, items)  # Hugging Face's count
                try:
                    progress.xet_report(key, report)
                except Exception:  # never let the progress bar break a download
                    logger.debug("Xet progress report not understood", exc_info=True)
            return result

        group = self._session.new_file_download_group(*args, progress_callback=callback, **kwargs)
        return _XetGroup(group, lambda: progress.set_ahead(key, 0))


class _XetGroup:
    """A Xet download group that forgets its share when it ends, however it ends."""

    def __init__(self, group, on_end: Callable[[], None]):
        self._group = group
        self._on_end = on_end

    def __getattr__(self, name):
        return getattr(self._group, name)

    def __enter__(self):
        return self._group.__enter__()

    def __exit__(self, *exc):
        try:
            return self._group.__exit__(*exc)
        finally:
            self._on_end()


def track(repo: str, hub: str) -> Optional[DownloadProgress]:
    """A DownloadProgress when the app reads progress lines, else None."""
    return DownloadProgress(repo, hub) if enabled() else None


def hf_kwargs(progress: Optional[DownloadProgress], snapshot_download: Callable) -> dict:
    """Extra arguments for Hugging Face's snapshot_download, when there is a progress to feed."""
    if progress is None or not accepts(snapshot_download, "tqdm_class"):
        return {}
    return {"tqdm_class": progress.hf_tqdm_class()}


def ms_kwargs(progress: Optional[DownloadProgress], snapshot_download: Callable) -> dict:
    """Extra arguments for ModelScope's snapshot_download, when there is a progress to feed."""
    if progress is None or not accepts(snapshot_download, "progress_callbacks"):
        return {}
    try:
        return {"progress_callbacks": [progress.ms_callback_class()]}
    except ImportError:
        return {}


class _Nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def reporting(progress: Optional[DownloadProgress]):
    """``with reporting(track(repo, hub)) as progress:``; a no-op when not tracking."""
    return progress if progress is not None else _Nothing()

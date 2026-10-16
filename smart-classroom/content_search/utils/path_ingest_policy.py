#
# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#

"""Denylist for ``/object/ingest-path`` source files.

Path ingest copies any file the backend account can read into the object store,
where it becomes downloadable. The endpoint is desktop-app only and the extension
allowlist already rejects config/credential formats; this adds a location check so
that system directories, application data, hidden directories and this product's
own install tree are never ingested, without restricting where users keep their
teaching material.

Kept free of ``utils.*`` imports so it can be unit-tested from the repo root.
"""

import functools
import os
import pathlib
import re
import socket
import sys
import tempfile
from typing import Iterable, List, Optional

# smart-classroom/ (code, config.yaml, .env, .proxy-config, logs).
_INSTALL_ROOT = pathlib.Path(__file__).resolve().parents[2]

_WINDOWS_DENIED_ENV = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                       "ProgramData", "APPDATA", "LOCALAPPDATA")
_POSIX_DENIED = ("/etc", "/proc", "/sys", "/dev", "/boot", "/root", "/var", "/usr",
                 "/bin", "/sbin", "/lib", "/lib64")

_DRIVE_RE = re.compile(r"^[a-zA-Z]:$")
_UNC_LOOPBACK = {"localhost", ".", "?", "::1", "0--1.ipv6-literal.net"}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _strip_extended_prefix(path: str) -> str:
    """``\\\\?\\C:\\x`` -> ``C:\\x``, ``\\\\?\\UNC\\h\\s`` -> ``\\\\h\\s``. resolve() keeps them."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[len("\\\\?\\UNC\\"):]
    if path.startswith("\\\\?\\"):
        return path[len("\\\\?\\"):]
    return path


class PathIngestPolicy:
    def __init__(self, denied_roots: Iterable[str], allowed_roots: Iterable[str] = (),
                 windows: bool = os.name == "nt"):
        self._denied = [_norm(r) for r in denied_roots if r]
        # Exceptions inside denied roots (temp dir under AppData, session storage
        # under the install tree). They do not override the hidden/UNC rules.
        self._allowed = [_norm(r) for r in allowed_roots if r]
        self._windows = windows
        self._local_hosts = _UNC_LOOPBACK | {socket.gethostname().lower()}

    def check(self, resolved: pathlib.Path) -> Optional[str]:
        """Return a rejection reason for an already ``resolve()``-d path, or None."""
        raw = str(resolved)
        if self._windows:
            raw = _strip_extended_prefix(raw)
            drive = pathlib.PureWindowsPath(raw).drive
            if drive.startswith("\\\\"):
                host, _, share = drive[2:].partition("\\")
                host = host.lower()
                if share.endswith("$") or host in self._local_hosts or host.startswith("127."):
                    return "administrative or loopback network share"
            elif not _DRIVE_RE.match(drive):
                return "device path"
            parts = pathlib.PureWindowsPath(raw).parts[1:]
        else:
            parts = pathlib.PurePosixPath(raw).parts[1:]

        if any(part.startswith(".") for part in parts):
            return "hidden file or directory"

        path = _norm(raw)
        if any(_is_under(path, root) for root in self._allowed):
            return None
        if any(_is_under(path, root) for root in self._denied):
            return "protected system or application directory"
        return None


def _resolved(paths: Iterable[Optional[os.PathLike]]) -> List[str]:
    out = []
    for p in paths:
        if not p:
            continue
        try:
            out.append(str(pathlib.Path(p).resolve()))
        except OSError:
            continue
    return out


@functools.lru_cache(maxsize=1)
def default_policy() -> PathIngestPolicy:
    denied = [_INSTALL_ROOT, sys.prefix]
    if os.name == "nt":
        denied += [os.environ.get(v) for v in _WINDOWS_DENIED_ENV]
        denied.append(pathlib.Path.home() / "AppData")
    else:
        denied += list(_POSIX_DENIED)
    # Explorer extracts files dragged out of zip archives / mail attachments into
    # the temp dir; main.py writes class sessions to <install>/storage by default.
    allowed = [tempfile.gettempdir(), _INSTALL_ROOT / "storage"]
    return PathIngestPolicy(_resolved(denied), _resolved(allowed))

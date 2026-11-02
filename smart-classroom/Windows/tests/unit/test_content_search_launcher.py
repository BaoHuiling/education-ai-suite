"""Regression tests for content_search/start_services.py, the launcher that
starts and relays the content-search services.

Both bugs left file-ingest stuck on "Starting" with nothing useful in its log:

  1. The thread relaying a service's output printed each line to the
     launcher's console before logging it, inside a bare `except: pass`. On a
     console that cannot encode a character (GBK, and the U+2705 torch.onnx
     prints while file-ingest exports CLIP) print() raised, the thread ended,
     and file-ingest blocked on its next write once the pipe buffer filled.

  2. Git for Windows' usr\\bin on PATH: python-magic, imported by unstructured
     in file-ingest, loads msys-magic-1.dll from it, which needs the MSYS
     runtime and hangs or crashes a native process.
"""

import builtins
import io
import os

import pytest


@pytest.fixture(scope="module")
def launcher():
    # Importing the launcher changes the working directory; put it back.
    cwd = os.getcwd()
    try:
        from content_search import start_services
    finally:
        os.chdir(cwd)
    return start_services


# --- 1. the relay that stopped draining --------------------------------------


def test_relay_keeps_draining_when_the_console_cannot_print(launcher, monkeypatch):
    real_print = builtins.print

    def gbk_console_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        text.encode("gbk")  # raises UnicodeEncodeError for U+2705, as a GBK console does
        real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", gbk_console_print)
    lines = [
        "Obtain model graph ...\n",
        "Obtain model graph ... ✅\n",
        "Translate the graph into ONNX...\n",
    ]
    log = io.StringIO()

    launcher._relay("ingest", iter(lines), log)

    # Every line was consumed and logged, including the one the console rejected.
    assert log.getvalue().splitlines() == [f"[ingest] {line.rstrip()}" for line in lines]


def test_relay_stops_quietly_when_the_pipe_closes(launcher):
    def closed_pipe():
        yield "last line\n"
        raise ValueError("I/O operation on closed file.")

    log = io.StringIO()
    launcher._relay("ingest", closed_pipe(), log)
    assert log.getvalue() == "[ingest] last line\n"


# --- 2. MSYS/Cygwin runtimes on PATH ----------------------------------------


@pytest.mark.parametrize("dll", ["msys-2.0.dll", "cygwin1.dll"])
def test_path_drops_folders_with_a_unix_runtime(launcher, tmp_path, dll):
    git_usr_bin = tmp_path / "Git" / "usr" / "bin"
    git_usr_bin.mkdir(parents=True)
    (git_usr_bin / dll).write_bytes(b"")
    (git_usr_bin / "msys-magic-1.dll").write_bytes(b"")
    plain = tmp_path / "tools"
    plain.mkdir()

    path = os.pathsep.join([str(plain), str(git_usr_bin), str(tmp_path / "missing")])
    assert launcher._without_unix_runtimes(path).split(os.pathsep) == [str(plain), str(tmp_path / "missing")]


def test_child_environment_has_no_unix_runtime_on_path(launcher, tmp_path, monkeypatch):
    git_usr_bin = tmp_path / "usr" / "bin"
    git_usr_bin.mkdir(parents=True)
    (git_usr_bin / "msys-2.0.dll").write_bytes(b"")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), str(git_usr_bin)]))

    env = launcher._build_env()

    if os.name == "nt":
        assert str(git_usr_bin) not in env["PATH"].split(os.pathsep)
    assert str(tmp_path) in env["PATH"].split(os.pathsep)
    assert env["PYTHONIOENCODING"] == "utf-8"

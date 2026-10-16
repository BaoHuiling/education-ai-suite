"""Child process that runs one GStreamer pipeline via the Python bindings.

Executes a pipeline description. The description itself is handed over verbatim
and fed to Gst.parse_launch. As a Python process it reports what the pipeline
is doing over a real IPC channel.

Launched by VideoAnalyticsPipelineService as:

    python -m components.va.pipeline_runner --name front --ipc <addr> --authkey-file <path>

Keep the import surface of this module tiny: every import here is paid again on
every pipeline launch and every retry.
"""

import argparse
import logging
import os
import sys
import threading
import time
from multiprocessing.connection import Client

# utils.gstreamer_env imports only the standard library.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.gstreamer_env import (  # noqa: E402
    GStreamerEnvError,
    ensure_dlstreamer_env,
    ensure_gst_registry,
    ensure_python_gst_env,
)

logger = logging.getLogger("va.runner")

# How long to keep waiting for EOS to reach the sinks after a stop request
# before giving up and tearing the pipeline down anyway.
STOP_EOS_TIMEOUT_SEC = 15.0

EXIT_EOS = 0
EXIT_ERROR = 1
EXIT_SETUP_FAILED = 2

# Resuming a file source: how long to wait for data to reach the probe, and for
# preroll at the new position.
RESUME_TIMEOUT_SEC = 30.0
# Seeking right up to (or past) the end leaves the pipeline waiting for an EOS
# that never comes, so a resume point closer to the end than this seeks back to
# the margin instead; the probe still drops the overlap, and the file runs out
# into a normal EOS.
RESUME_END_MARGIN_SEC = 0.5


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Run one DL Streamer pipeline.")
    parser.add_argument("--name", required=True, help="Pipeline name (front, back, content)")
    parser.add_argument("--ipc", required=True, help="Address of the parent's connection listener")
    parser.add_argument(
        "--authkey-file",
        required=True,
        help="File holding the IPC authkey. Read then deleted; never passed on the command line.",
    )
    return parser.parse_args(argv)


def _connect(address, authkey_file):
    """Connect back to the parent, consuming the authkey file."""
    with open(authkey_file, "rb") as fh:
        authkey = fh.read()
    try:
        os.unlink(authkey_file)
    except OSError:
        pass
    return Client(address, authkey=authkey)


def _watch_for_stop(conn, on_stop):
    """Read commands from the parent until the connection closes."""
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if isinstance(message, dict) and message.get("cmd") == "stop":
            on_stop()
            return


def _run_bus_loop(Gst, pipeline, conn, stopping):
    """Pump the bus until EOS or ERROR. Returns the process exit code."""
    bus = pipeline.get_bus()
    interesting = (
        Gst.MessageType.EOS
        | Gst.MessageType.ERROR
        | Gst.MessageType.WARNING
        | Gst.MessageType.STATE_CHANGED
    )
    announced_playing = False

    while True:
        message = bus.timed_pop_filtered(Gst.SECOND, interesting)
        if message is None:
            # Timed out with nothing to report. Check whether the stop deadline
            # passed while we were waiting for EOS to drain through the sinks.
            if stopping.is_set() and stopping.expired():
                logger.warning("EOS did not arrive within %.0fs of stop; forcing teardown",
                               STOP_EOS_TIMEOUT_SEC)
                return EXIT_EOS
            continue

        if message.type == Gst.MessageType.EOS:
            logger.info("EOS")
            conn.send({"ev": "eos"})
            return EXIT_EOS

        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            element = message.src.get_name() if message.src else None
            logger.error("ERROR from %s: %s | %s", element, error.message, debug)
            conn.send({
                "ev": "error",
                "element": element,
                "message": error.message,
                "debug": debug,
                "domain": error.domain,
                "code": error.code,
            })
            return EXIT_ERROR

        if message.type == Gst.MessageType.WARNING:
            error, debug = message.parse_warning()
            element = message.src.get_name() if message.src else None
            logger.warning("WARNING from %s: %s | %s", element, error.message, debug)
            conn.send({
                "ev": "warning",
                "element": element,
                "message": error.message,
                "debug": debug,
            })
            continue

        # STATE_CHANGED: only the pipeline's own transition to PLAYING matters.
        # This is the signal the parent waits on instead of sleeping 5 seconds.
        if not announced_playing and message.src is pipeline:
            _old, new, _pending = message.parse_state_changed()
            if new == Gst.State.PLAYING:
                announced_playing = True
                logger.info("PLAYING")
                conn.send({"ev": "playing"})


def _drain_error(Gst, pipeline, timeout_sec=2.0):
    """Pull the ERROR that explains a failed state change off the bus."""
    message = pipeline.get_bus().timed_pop_filtered(
        int(timeout_sec * Gst.SECOND), Gst.MessageType.ERROR
    )
    if message is None:
        return {
            "ev": "error",
            "element": None,
            "message": "Pipeline refused to start (set_state PLAYING returned FAILURE)",
            "debug": None,
        }
    error, debug = message.parse_error()
    return {
        "ev": "error",
        "element": message.src.get_name() if message.src else None,
        "message": error.message,
        "debug": debug,
        "domain": error.domain,
        "code": error.code,
    }


def _exit_now(code: int) -> None:
    """Leave the process without running DLL detach handlers.

    Normal exit paths -- sys.exit, os._exit, even Gst.deinit() first -- all
    end in ExitProcess, which runs DllMain(DLL_PROCESS_DETACH) on every loaded
    module. On this platform the Level Zero driver unloads before the OpenVINO
    NPU plugin's destructors run, so those destructors tear down command lists
    against a dead driver, and the process dies with STATUS_STACK_BUFFER_OVERRUN (0xC0000409). 

    Safe here because the pipeline is already in NULL state: GStreamer has run
    every element's stop(), the metadata files are closed, and the parent has
    its terminal event.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateProcess.restype = wintypes.BOOL
            # -1 is the pseudo-handle for the current process.
            kernel32.TerminateProcess(ctypes.c_void_p(-1), code)
        except Exception:
            pass  # Fall through to os._exit if the call could not be made.

    os._exit(code)


def _teardown(Gst, pipeline):
    """Stop the pipeline so the sinks flush and close their output files."""
    result = pipeline.set_state(Gst.State.NULL)
    logger.info("Teardown: set_state(NULL) -> %s", result.value_nick)


def _resume_at(Gst, pipeline, position_ns, probe_element):
    """Pre-position a file pipeline at position_ns, leaving it PAUSED.

    Preroll starts decoding from the top of the file, so until the seek lands a
    probe on probe_element's src pad drops every frame before position_ns;
    otherwise those frames would reach the metadata files a second time. A plain
    drop without the seek is no substitute: the segment would still start at 0,
    and synced sinks would hold the first frame until the clock caught up.

    Raises RuntimeError when the pipeline cannot be positioned.
    """
    element = pipeline.get_by_name(probe_element)
    if element is None:
        raise RuntimeError(f"resume probe element '{probe_element}' not found")
    pad = element.get_static_pad("src")
    data_flowing = threading.Event()
    drop_before = [position_ns]  # mutable: lowered below for a point past the end

    def _drop_early(_pad, info):
        data_flowing.set()
        buf = info.get_buffer()
        if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE and buf.pts < drop_before[0]:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, _drop_early)
    if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("set_state PAUSED failed")
    # The seek needs the demuxer and decoder running; the first frame at the
    # probe says they are.
    if not data_flowing.wait(RESUME_TIMEOUT_SEC):
        raise RuntimeError(f"no decoded frame within {RESUME_TIMEOUT_SEC:.0f}s")

    ok, duration = pipeline.query_duration(Gst.Format.TIME)
    margin = int(RESUME_END_MARGIN_SEC * Gst.SECOND)
    seek_ns = position_ns
    if ok and duration > margin and position_ns > duration - margin:
        seek_ns = duration - margin
        logger.info("Resume point %.1fs is within %.1fs of the end (%.1fs); seeking to %.1fs",
                    position_ns / Gst.SECOND, RESUME_END_MARGIN_SEC,
                    duration / Gst.SECOND, seek_ns / Gst.SECOND)
        if position_ns >= duration:
            # Nothing would pass the probe, and an RTSP sink that never sees a
            # frame never finishes on EOS. Let the tail through: at most the
            # margin is written twice, which beats a hang.
            drop_before[0] = seek_ns

    if not pipeline.seek_simple(Gst.Format.TIME,
                                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, seek_ns):
        raise RuntimeError(f"seek to {seek_ns / Gst.SECOND:.1f}s failed")
    ret, _state, _pending = pipeline.get_state(int(RESUME_TIMEOUT_SEC * Gst.SECOND))
    if ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("preroll after seek failed")
    if seek_ns == drop_before[0]:
        # Every frame from here is past the drop threshold; drop the per-frame callback.
        pad.remove_probe(probe_id)
    logger.info("Resumed at %.1fs", position_ns / Gst.SECOND)


class _StopState:
    """Stop request plus the deadline for EOS to make it through the pipeline."""

    def __init__(self, timeout):
        self._event = threading.Event()
        self._timeout = timeout
        self._deadline = None

    def request(self):
        self._deadline = time.monotonic() + self._timeout
        self._event.set()

    def is_set(self):
        return self._event.is_set()

    def expired(self):
        return self._deadline is not None and time.monotonic() >= self._deadline


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    global logger
    logger = logging.getLogger(f"va.runner.{args.name}")

    try:
        conn = _connect(args.ipc, args.authkey_file)
    except Exception as e:
        logger.error("Failed to connect to parent at %s: %s", args.ipc, e)
        return EXIT_SETUP_FAILED

    try:
        try:
            # GST_PLUGIN_PATH and GST_REGISTRY are normally inherited from the
            # parent; these calls are idempotent and make the runner usable
            # standalone for debugging.
            ensure_gst_registry()
            ensure_dlstreamer_env()
            ensure_python_gst_env()

            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            Gst.init(None)
        except (GStreamerEnvError, ImportError, ValueError) as e:
            logger.error("GStreamer setup failed: %s", e)
            conn.send({"ev": "error", "element": None, "message": str(e), "debug": "runner setup"})
            return EXIT_SETUP_FAILED

        request = conn.recv()
        description = request["description"]
        logger.info("Pipeline description: %s", description)

        try:
            # Raises GLib.Error on a malformed description.
            pipeline = Gst.parse_launch(description)
        except Exception as e:
            logger.error("parse_launch failed: %s", e)
            conn.send({
                "ev": "error",
                "element": None,
                "message": f"Failed to parse pipeline description: {e}",
                "debug": description,
            })
            return EXIT_ERROR

        stopping = _StopState(STOP_EOS_TIMEOUT_SEC)

        def _on_stop():
            # Same clean shutdown `gst-launch -e` performs on Ctrl-C: let EOS
            # travel the pipeline so sinks and muxers finalise their output.
            logger.info("Stop requested; sending EOS")
            stopping.request()
            pipeline.send_event(Gst.Event.new_eos())

        threading.Thread(
            target=_watch_for_stop, args=(conn, _on_stop), daemon=True, name="ipc-reader"
        ).start()

        resume_ns = request.get("start_position_ns")
        if resume_ns:
            try:
                _resume_at(Gst, pipeline, int(resume_ns), request["resume_probe_element"])
            except Exception as e:
                logger.error("Resume at %.1fs failed: %s", int(resume_ns) / Gst.SECOND, e)
                conn.send({"ev": "error", "element": None,
                           "message": f"Resume failed: {e}", "debug": "runner resume"})
                pipeline.set_state(Gst.State.NULL)
                return EXIT_ERROR

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            error = _drain_error(Gst, pipeline)
            logger.error("Failed to set pipeline to PLAYING: %s", error["message"])
            conn.send(error)
            pipeline.set_state(Gst.State.NULL)
            return EXIT_ERROR

        try:
            return _run_bus_loop(Gst, pipeline, conn, stopping)
        finally:
            _teardown(Gst, pipeline)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    _exit_now(main())

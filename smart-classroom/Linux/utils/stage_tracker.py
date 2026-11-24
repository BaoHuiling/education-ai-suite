import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from utils.stage_events import StageEventWriter

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StageHandle:
    """Escape hatch for stages that report failure in-band instead of raising.

    A streaming endpoint that catches its own errors and yields them as a payload
    (so the browser gets a message rather than a truncated response) leaves
    nothing for the context manager to see. Such a stage calls `fail(exc)` to say
    so explicitly; everyone else can ignore the handle.
    """

    def __init__(self):
        self.error = None

    def fail(self, exc) -> None:
        if self.error is None:
            self.error = exc


@contextmanager
def stage_tracker(session_id, stage):
    """Wrap a pipeline stage: record start/end/duration/exception to both the
    log and the per-session stage_events.jsonl. One instrumentation point, two
    outlets. Re-raises on failure so the caller's error handling still runs.
    """
    started_at = _now_iso()
    t0 = time.monotonic()
    logger.info(f"[stage] {session_id} {stage} start")
    handle = StageHandle()
    try:
        yield handle
    except Exception as e:
        duration = round(time.monotonic() - t0, 3)
        StageEventWriter.write(
            session_id, stage, "failed", started_at, _now_iso(), duration,
            error_class=type(e).__name__, error_detail=str(e),
        )
        logger.exception(f"[stage] {session_id} {stage} failed after {duration}s")
        raise
    else:
        duration = round(time.monotonic() - t0, 3)
        if handle.error is None:
            StageEventWriter.write(
                session_id, stage, "done", started_at, _now_iso(), duration,
            )
            logger.info(f"[stage] {session_id} {stage} done in {duration}s")
        else:
            exc = handle.error
            StageEventWriter.write(
                session_id, stage, "failed", started_at, _now_iso(), duration,
                error_class=type(exc).__name__, error_detail=str(exc),
            )
            # exc_info=exc rather than logger.exception(): a stage that reported
            # its failure through StageHandle is not inside an except block.
            logger.error(
                f"[stage] {session_id} {stage} failed after {duration}s", exc_info=exc
            )

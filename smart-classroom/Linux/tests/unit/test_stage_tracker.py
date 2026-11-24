from unittest.mock import patch

from utils.stage_tracker import stage_tracker


def test_success_writes_done_event():
    with patch("utils.stage_tracker.StageEventWriter.write") as mw:
        with stage_tracker("s1", "summarize"):
            pass
    assert mw.called
    args, kwargs = mw.call_args
    # positional: session_id, stage, status, started_at, ended_at, duration
    assert args[0] == "s1"
    assert args[1] == "summarize"
    assert args[2] == "done"
    assert args[5] >= 0  # duration
    assert kwargs.get("error_class") is None


def test_failure_writes_failed_event_and_reraises():
    raised = False
    with patch("utils.stage_tracker.StageEventWriter.write") as mw:
        try:
            with stage_tracker("s1", "mindmap"):
                raise ValueError("boom")
        except ValueError:
            raised = True
    assert raised  # exception propagated
    args, kwargs = mw.call_args
    assert args[2] == "failed"
    assert kwargs.get("error_class") == "ValueError"
    assert kwargs.get("error_detail") == "boom"


def test_in_band_failure_via_handle_writes_failed_event_without_raising():
    with patch("utils.stage_tracker.StageEventWriter.write") as mw:
        with stage_tracker("s1", "report") as stage:
            stage.fail(RuntimeError("bad template"))
            stage.fail(ValueError("second error is ignored"))
    args, kwargs = mw.call_args
    assert mw.call_count == 1
    assert args[1:3] == ("report", "failed")
    assert kwargs.get("error_class") == "RuntimeError"
    assert kwargs.get("error_detail") == "bad template"

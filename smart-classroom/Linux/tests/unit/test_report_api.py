"""Report API (ported from Windows): generate / reselect / download / field catalog.

Runs the real ReportGenerator against the built-in .docx template with a fake
text_gen, on session data shaped like what the Linux services write (ASR
teacher transcript, summary.md, VA class_statistics.json).
"""

import json
import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.report import router
from utils.session_paths import SessionPaths

SID = "20261124-101500-abcd"

LLM_FIELDS = {
    "interaction_level": "LLM-INTERACTION-TEXT",
    "classroom_atmosphere": "LLM-ATMOSPHERE-TEXT",
    "recommendations": "LLM-RECOMMENDATIONS-TEXT",
}


class FakeTextGen:
    def __init__(self, response=None, fail_if_called=False):
        self.response = json.dumps(LLM_FIELDS) if response is None else response
        self.fail_if_called = fail_if_called
        self.calls = 0

    def generate(self, *args, **kwargs):
        if self.fail_if_called:
            raise AssertionError("text_gen must not be called")
        self.calls += 1
        return self.response


class FakeModelManager:
    def __init__(self, handler):
        self.handler = handler

    def text_gen(self):
        return self.handler


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def session():
    raw = SessionPaths.raw_dir(SID)
    result = SessionPaths.result_dir(SID)
    va = SessionPaths.va_dir(SID)
    for d in (raw, result, va):
        d.mkdir(parents=True, exist_ok=True)

    SessionPaths.summary_path(SID).write_text(
        "## Summary\nA lesson on Newton's laws.\n\n"
        "## Keywords\n- Newton, force, acceleration\n\n"
        "## Key Difficulty Points\n- Newton's second law\n",
        encoding="utf-8",
    )
    SessionPaths.teacher_transcript_path(SID).write_text(
        "[0.0 - 40.0] Today we study Newton's second law.\n"
        "[40.0 - 80.0] What is the relation between force and acceleration?\n"
        "[80.0 - 120.0] Newton's second law says F equals m a.\n",
        encoding="utf-8",
    )
    SessionPaths.class_statistics_path(SID).write_text(
        json.dumps({"student_count": 30, "stand_count": 2, "raise_up_count": 5,
                    "duration_sec": 120.0}),
        encoding="utf-8",
    )
    (va / "front_posture.txt").write_text("{}\n", encoding="utf-8")
    return SID


def _use_text_gen(handler):
    return patch("pipeline.ModelManager.instance", return_value=FakeModelManager(handler))


def _events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _generate(client, **body):
    return client.post("/report/generate", json={"session_id": SID, **body})


def test_template_fields_lists_groups_with_manual_defaults(client):
    groups = client.get("/report/template-fields").json()["groups"]
    fields = [f for g in groups for f in g["fields"]]
    codes = {f["code"] for f in fields}
    assert {"attendance", "keywords", "recommendations", "mindmap"} <= codes
    manual = [f for f in fields if f.get("input") == "manual"]
    assert manual and all("default" in f for f in manual)


def test_generate_fills_raw_and_llm_fields(client, session):
    handler = FakeTextGen()
    with _use_text_gen(handler), \
            patch("utils.stage_tracker.StageEventWriter.write") as stage_write:
        resp = _generate(client)
    assert resp.status_code == 200
    events = _events(resp)
    types = [e.get("type") for e in events]
    assert types[0] == "partial_report"
    assert types[-2:] == ["report", "report_ready"]
    assert not any(e.get("error") for e in events)

    report = events[-2]["content"]
    assert "LLM-RECOMMENDATIONS-TEXT" in report
    assert "Newton" in report  # keyword pulled from summary.md, not the LLM
    assert handler.calls == 1

    assert SessionPaths.report_docx_path(SID).exists()
    assert SessionPaths.report_md_path(SID).read_text(encoding="utf-8") == report
    assert stage_write.call_args.args[1:3] == ("report", "done")

    got = client.get(f"/report/{SID}").json()
    assert got["report"] == report.strip()  # read_text_file strips


def test_reselect_drops_field_without_calling_llm(client, session):
    with _use_text_gen(FakeTextGen()):
        _generate(client)
    with _use_text_gen(FakeTextGen(fail_if_called=True)):
        groups = client.get("/report/template-fields").json()["groups"]
        codes = [f["code"] for g in groups for f in g["fields"]]
        keep = [c for c in codes if c != "recommendations"]
        resp = client.post(f"/report/{SID}/reselect", json={"selected_fields": keep})
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert "LLM-RECOMMENDATIONS-TEXT" not in report
    assert "LLM-ATMOSPHERE-TEXT" in report


def test_llm_error_keeps_report_with_na_generated_fields(client, session):
    with _use_text_gen(FakeTextGen(response="[ERROR]: backend down")), \
            patch("utils.stage_tracker.StageEventWriter.write") as stage_write:
        events = _events(_generate(client))
    assert [e.get("type") for e in events][-2:] == ["report", "report_ready"]
    report = events[-2]["content"]
    assert "LLM-" not in report
    assert "N/A" in report
    assert stage_write.call_args.args[2] == "done"


def test_in_band_error_marks_stage_failed(client, session):
    with _use_text_gen(FakeTextGen()), \
            patch("components.report_generator.report_generator.get_template_path",
                  return_value=None), \
            patch("utils.stage_tracker.StageEventWriter.write") as stage_write:
        events = _events(_generate(client))
    assert events[-1]["error"].startswith("[ERROR]:")
    args, kwargs = stage_write.call_args
    assert args[1:3] == ("report", "failed")
    assert kwargs["error_class"] == "RuntimeError"


def test_download_docx_and_pdf_unavailable(client, session):
    with _use_text_gen(FakeTextGen()):
        _generate(client)
    docx = client.get(f"/report/{SID}/download")
    assert docx.status_code == 200
    assert docx.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert docx.content[:2] == b"PK"

    with patch("components.report_generator.pdf_export.pdf_export_available", return_value=False):
        assert client.get("/report/capabilities").json() == {"pdf_export": False}
        pdf = client.get(f"/report/{SID}/download", params={"format": "pdf"})
    assert pdf.status_code == 501
    assert "DOCX" in pdf.json()["detail"]


def _fake_render(calls):
    def render(markdown_text, output_path, mindmap_image_path=None):
        calls.append((markdown_text, mindmap_image_path))
        with open(output_path, "wb") as f:
            f.write(b"%PDF-1.7 fake")
        return output_path
    return render


def test_pdf_rendered_from_markdown_and_cached(client, session):
    with _use_text_gen(FakeTextGen()):
        _generate(client)
    calls = []
    with patch("components.report_generator.pdf_export.pdf_export_available", return_value=True),             patch("components.report_generator.pdf_export.markdown_to_pdf", _fake_render(calls)):
        assert client.get("/report/capabilities").json() == {"pdf_export": True}
        pdf = client.get(f"/report/{SID}/download", params={"format": "pdf"})
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")
        assert len(calls) == 1
        assert calls[0][0] == SessionPaths.report_md_path(SID).read_text(encoding="utf-8").strip()
        assert calls[0][1] == str(SessionPaths.mindmap_png_path(SID))

        # Unchanged report: served from the cache, not rendered again.
        assert client.get(f"/report/{SID}/download", params={"format": "pdf"}).status_code == 200
        assert len(calls) == 1

        # A newer markdown (e.g. after reselect) invalidates the cache.
        md = SessionPaths.report_md_path(SID)
        pdf_file = SessionPaths.result_dir(SID) / f"class_report_{SID}.pdf"
        newer = pdf_file.stat().st_mtime + 5
        os.utime(md, (newer, newer))
        assert client.get(f"/report/{SID}/download", params={"format": "pdf"}).status_code == 200
        assert len(calls) == 2


def test_pdf_render_failure_returns_500(client, session):
    with _use_text_gen(FakeTextGen()):
        _generate(client)

    def boom(*args, **kwargs):
        raise RuntimeError("pango exploded")

    with patch("components.report_generator.pdf_export.pdf_export_available", return_value=True),             patch("components.report_generator.pdf_export.markdown_to_pdf", boom):
        pdf = client.get(f"/report/{SID}/download", params={"format": "pdf"})
    assert pdf.status_code == 500
    assert pdf.json()["detail"] == "Failed to convert report to PDF."


def test_report_missing_returns_404(client, session):
    assert client.get(f"/report/{SID}").status_code == 404
    assert client.get(f"/report/{SID}/download").status_code == 404


def test_mindmap_image_roundtrip(client, session):
    png = b"\x89PNG\r\n\x1a\nfake"
    up = client.post(f"/report/{SID}/mindmap-image",
                     files={"file": ("mm.png", png, "image/png")})
    assert up.status_code == 200
    assert SessionPaths.mindmap_png_path(SID).read_bytes() == png
    assert client.get(f"/report/{SID}/mindmap-image").content == png

    empty = client.post(f"/report/{SID}/mindmap-image",
                        files={"file": ("mm.png", b"", "image/png")})
    assert empty.status_code == 400


@pytest.mark.parametrize("bad", ["../etc", ".hidden", "a/b", ""])
def test_generate_rejects_unsafe_session_id(client, bad):
    resp = client.post("/report/generate", json={"session_id": bad})
    assert resp.status_code == 422


def test_path_routes_reject_unsafe_session_id(client):
    assert client.get("/report/.hidden").status_code == 422
    assert client.get("/report/.hidden/download").status_code == 422
    assert client.post("/report/.hidden/reselect", json={}).status_code == 422

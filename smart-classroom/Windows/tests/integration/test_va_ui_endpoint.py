"""The UI's video-analytics endpoint (/start-video-analytics-pipeline).

When every pipeline finishes, the done callback must save the VA outputs
(class_statistics.json) before it settles the va stage: settling it can
complete the session, and segmentation and the report start on it. It used
to settle first, so they could read a missing stats file.
"""

from unittest.mock import MagicMock, patch

from api import endpoints
from utils.session_manager import generate_session_id


def test_done_callback_saves_outputs_before_settling_the_stage(client):
    service = MagicMock(name="VideoAnalyticsPipelineService")
    service.is_pipeline_running.return_value = False
    service.launch_pipeline.return_value = True
    service.pipeline_final_status = {"front": "eos"}

    calls = []
    sid = generate_session_id()
    with patch.object(endpoints, "ensure_media_service_running"), \
         patch.object(endpoints, "VideoAnalyticsPipelineService", return_value=service), \
         patch.object(endpoints, "save_va_outputs", lambda svc, session_id: calls.append("save")), \
         patch.object(endpoints, "_set_va_stage",
                      lambda session_id, status, detail=None: calls.append(f"stage:{status}")):
        try:
            resp = client.post(
                "/start-video-analytics-pipeline",
                headers={"X-Session-ID": sid},
                json=[{"pipeline_name": "front", "source": "rtsp://127.0.0.1:8554/live", "output_stream": False}],
            )
            assert resp.status_code == 200, resp.text
            assert [r["status"] for r in resp.json()["results"]] == ["success"]

            service.on_all_pipelines_done(sid)
        finally:
            endpoints.va_services.pop(sid, None)

    assert calls == ["stage:running", "save", "stage:done"]

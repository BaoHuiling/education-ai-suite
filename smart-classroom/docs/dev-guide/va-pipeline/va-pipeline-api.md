<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# API Reference Guide - Video Analytics

The Video Analytics (VA) pipeline analyzes classroom camera feeds (front, back,
content) for pose, posture, student re-identification, and class-engagement
statistics, and publishes annotated streams for in-browser playback. On Linux it
runs as a Docker side-car driven by the Smart Classroom app over HTTP.

This guide documents the **application-facing API** (served by
`smart-classroom-app` on `:8000`) that the UI calls, followed by the internal
**side-car control API** for reference.

## Availability and Feature Switch

- Feature id: `video_analytics`
- Config switch: `features.video_analytics.enabled` in `config.yaml`
- When enabled, `setup.sh` activates the `va` Compose profile, bringing up the
  `video-analytics` and `mediamtx` side-cars.
- When disabled, the `/*-video-analytics-*`, `/class-statistics`, and
  `/upload-video` routes are not mounted.

## Global Request Specification

All session-scoped routes require the session header:

| Header | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `x-session-id` | String | Yes | Session identifier. Must match `^[A-Za-z0-9_-]{1,128}$` (used as a filesystem path segment). |

Pipeline names are exactly: `front`, `back`, `content`.

A pipeline **source** must be one of:
- an `rtsp://` / `rtsps://` / `rtspt://` URL (no whitespace, no `" \ !` or control chars), or
- the container path returned by `POST /upload-video` for the same session.

Any other source is rejected (prevents arbitrary-file access and GStreamer
pipeline injection).

## Status Codes

### HTTP Status Codes

| Code | Meaning | Frontend Handling Suggestion |
| :--- | :--- | :--- |
| 200 | OK | Parse the JSON / NDJSON body. |
| 201 | Created | Upload persisted. |
| 400 | Bad Request | Missing/invalid `x-session-id`, empty body, invalid `pipeline_name`, invalid/empty/unsafe `source`, or bad file type. |
| 404 | Not Found | Unknown route / pipeline not registered. |
| 500 | Server Error | Unexpected backend/side-car error. |

### Per-pipeline result status (in `results[]`)

| Status | Meaning |
| :--- | :--- |
| `success` | Pipeline launched / stopped. |
| `error` | See the `error` field (e.g. already running, invalid source, launch failed). |

### Pipeline lifecycle status (in the monitor stream)

| Status | Meaning | Frontend Action |
| :--- | :--- | :--- |
| `running` | Pipeline process alive. | Keep streaming. |
| `not_found` | Pipeline not registered for this session. | Ignore / show idle. |
| `stopped_normal` | Reached end-of-stream. | Stop & show result. |
| `stopped_error` | Exited unexpectedly (may be retried internally). | Show warning; watch for restart. |

---

## API Endpoints

### Pipeline endpoints

#### Upload a video clip

* URL: /upload-video

* Method: POST

* Pattern: SYNC

  Form Parameters (`multipart/form-data`):
  | Parameter | Type | Required | Description |
  | :--- | :--- | :--- | :--- |
  | `file` | file | Yes | The video clip. Allowed: `.mp4`, `.avi`, `.mkv`, `.mov`, `.webm`. Max 4 GB (configurable). |
  | `pipeline_name` | string | Yes | `front`, `back`, or `content`. |

  Request:

  ```text
  curl --location 'http://127.0.0.1:8000/upload-video' \
    --header 'x-session-id: 20261026-120043-d16f' \
    --form 'pipeline_name=front' \
    --form 'file=@/path/to/front.mp4'
  ```

  Response (201 Created)

  ```json
  {
    "filename": "front__front.mp4",
    "pipeline_name": "front",
    "message": "Video uploaded successfully",
    "path": "/app/storage/smart-classroom/20261026-120043-d16f/raw/va/uploads/front__front.mp4"
  }
  ```

  Pass the returned `path` back as the `source` in `start-video-analytics-pipeline`.

#### Start pipelines

* URL: /start-video-analytics-pipeline

* Method: POST

* Pattern: SYNC

  Body: JSON array of pipeline requests.
  | Field | Type | Required | Description |
  | :--- | :--- | :--- | :--- |
  | `pipeline_name` | string | Yes | `front`, `back`, or `content`. |
  | `source` | string | Yes | Uploaded clip path (from `/upload-video`) or an `rtsp://` URL. |
  | `output_stream` | boolean | No (default `true`) | When `false`, the annotated video is discarded and no `stream_url` is returned. |

  Request:

  ```text
  curl --location 'http://127.0.0.1:8000/start-video-analytics-pipeline' \
    --header 'x-session-id: 20261026-120043-d16f' \
    --header 'Content-Type: application/json' \
    --data '[{"pipeline_name":"front","source":"rtsp://mediamtx:8554/front_cam","output_stream":true}]'
  ```

  Response (200 OK)

  ```json
  {
    "results": [
      {
        "status": "success",
        "pipeline_name": "front",
        "session_id": "20261026-120043-d16f",
        "output_stream": true,
        "stream_url": "http://localhost:8889/front_stream",
        "stream_protocol": "webrtc",
        "overlays_embedded": true
      }
    ]
  }
  ```

  On a rejected source:

  ```json
  { "results": [ { "status": "error", "pipeline_name": "front",
    "session_id": "20261026-120043-d16f",
    "error": "source contains invalid characters" } ] }
  ```

#### Stop pipelines

* URL: /stop-video-analytics-pipeline

* Method: POST

* Pattern: SYNC

  Body: JSON array of `{ "pipeline_name": "front" }`.

  Request:

  ```text
  curl --location 'http://127.0.0.1:8000/stop-video-analytics-pipeline' \
    --header 'x-session-id: 20261026-120043-d16f' \
    --header 'Content-Type: application/json' \
    --data '[{"pipeline_name":"front"}]'
  ```

  Response (200 OK)

  ```json
  { "results": [ { "status": "success", "pipeline_name": "front",
    "session_id": "20261026-120043-d16f" } ] }
  ```

### Streaming endpoints

#### Monitor pipeline status

* URL: /monitor-video-analytics-pipeline

* Method: GET

* Pattern: STREAM (NDJSON, `application/x-ndjson`)

  Request:

  ```text
  curl --no-buffer --location 'http://127.0.0.1:8000/monitor-video-analytics-pipeline' \
    --header 'x-session-id: 20261026-120043-d16f'
  ```

  Response (200 OK) — one JSON object per line:

  ```json
  {"pipelines": [
    {"pipeline_name": "front", "status": "running", "final_status": null, "pid": 35},
    {"pipeline_name": "back", "status": "not_found", "final_status": null, "message": "Pipeline 'back' not found"},
    {"pipeline_name": "content", "status": "not_found", "final_status": null, "message": "Pipeline 'content' not found"}
  ]}
  ```

  The stream ends once every pipeline that ran has settled.

#### Class statistics

* URL: /class-statistics

* Method: GET

* Pattern: STREAM (NDJSON, `application/x-ndjson`)

  Emits the cumulative classroom statistics (recomputed from `front_posture.txt`)
  every few seconds.

  Request:

  ```text
  curl --no-buffer --location 'http://127.0.0.1:8000/class-statistics' \
    --header 'x-session-id: 20261026-120043-d16f'
  ```

  Response (200 OK) — one JSON object per line:

  ```json
  {
    "student_count": 13,
    "stand_count": 1,
    "raise_up_count": 12,
    "stand_reid": [ {"student_id": 2, "count": 2} ],
    "raise_reid": [ {"student_id": 30, "count": 7} ]
  }
  ```

  | Field | Type | Description |
  | :--- | :--- | :--- |
  | `student_count` | integer | Detected students in the window. |
  | `stand_count` | integer | Stand-up events. |
  | `raise_up_count` | integer | Hand-raise events. |
  | `stand_reid` | array | Per-student stand counts (by ReID id). |
  | `raise_reid` | array | Per-student hand-raise counts (by ReID id). |

On session completion the app persists the final object to
`<session>/raw/va/class_statistics.json`.

---

## Output artifacts

Written under `<session>/raw/va/` in the shared storage volume (host:
`Linux/storage/smart-classroom/<session>/raw/va/`):

| File | Format | Pipeline | Contents |
| :--- | :--- | :--- | :--- |
| `front_posture.txt` | JSON-Lines | front | Pose/posture/ReID detections (statistics source) |
| `front_resnet18.txt`, `front_mobilenetv2.txt` | JSON-Lines | front | Classification metadata |
| `back_posture.txt`, `back_resnet18.txt` | JSON-Lines | back | Back pose + classification |
| `content_results.txt` | JSON-Lines | content | Content-camera classification |
| `class_statistics.json` | JSON | session | Aggregated statistics |
| `uploads/<pipeline>__<name>` | binary | — | Uploaded source clips |
| `logs/<pipeline>_<unixtime>.log` | text | — | Per-run pipeline log |

---

## Streaming playback (mediamtx)

| Port (host) | Protocol | Use |
| :--- | :--- | :--- |
| 8554 | RTSP | Side-car publishes the annotated stream; also where simulated camera sources are pushed. |
| 8889 | WebRTC | Browser playback: `{webrtc_base_url}/{name}_stream`. |
| 8890 → 8888 | HLS | Browser playback fallback (host port remapped to avoid conflicts). |

`stream_protocol` (config: `va_pipeline.stream_protocol`) selects which base URL
`start-video-analytics-pipeline` returns.

If the base URL host is `localhost`, the response uses the host the request was
sent to instead. For example, a UI opened as `http://10.0.0.5:8000` gets
`http://10.0.0.5:8889/front_stream`. Set a different host in the base URL to
override this.

WebRTC from another machine also needs the host IP as an ICE candidate. Inside
the Docker network, MediaMTX knows only its container IP, so a remote session
times out and the player stays black. `setup.sh` writes the host's default-route
IP to `VA_WEBRTC_ADDITIONAL_HOSTS` in `.env` (comma-separated, kept if already
set), and Compose passes it to MediaMTX as `MTX_WEBRTCADDITIONALHOSTS`.

---

## Internal side-car control API (reference)

The `video-analytics` side-car exposes a lower-level control API on `:8020`
(internal to the Compose network, **not published to the host**). The app talks
to it via `components/va/va_client`; direct use is for debugging only.

| Method | URL | Purpose |
| :--- | :--- | :--- |
| GET | `/health` | Liveness probe (`{"status":"ok","service":"video-analytics"}`). |
| POST | `/pipelines/{name}/launch` | Launch a pipeline. Body: `{source, device?, output_dir, output_rtsp, output_stream, threshold, record}`. |
| POST | `/pipelines/{name}/stop?timeout=10` | Stop a pipeline. |
| GET | `/pipelines` | All registered pipeline statuses. |
| GET | `/pipelines/{name}` | One pipeline status. |
| GET | `/pipelines/status?check_interval=2` | NDJSON status stream. |
| GET | `/class-statistics?front_posture_file=…&poll_interval=5` | NDJSON statistics stream. |

Example (debug, from inside the Compose network):

```text
curl 'http://video-analytics:8020/health'
curl -X POST 'http://video-analytics:8020/pipelines/front/launch' \
  -H 'Content-Type: application/json' \
  -d '{"source":"/app/sample-videos/front.mp4","device":"NPU","output_dir":"/app/smoke","output_rtsp":"rtsp://mediamtx:8554","output_stream":true,"threshold":0.5}'
```

---

## Configuration reference

```yaml
features:
  video_analytics: { enabled: true }

models:
  va:
    models_base_path: "models"
    device: NPU                 # NPU | GPU | CPU
    front_pose_model: yolov8m-pose
    back_pose_model: yolov8s-pose
    threshold: 0.5

va_pipeline:
  endpoint: http://video-analytics:8020    # resolved from VIDEO_ANALYTICS_URL first
  rtsp_codec: h264
  output_rtsp_url: rtsp://mediamtx:8554
  completion_timeout_sec: 3600
  stream_protocol: webrtc                  # webrtc | hls
  webrtc_base_url: http://localhost:8889
  hls_base_url: http://localhost:8888
  pose_statistics:
    min_frames_for_transition: 3
    min_frames_for_transition_unid: 15
    absence_threshold: 90
    min_stand_frames: 10
    center_dist_threshold: 0.1
    unidentified_max: 50
    stale_unidentified_threshold: 30
```

| Env var | Purpose |
| :--- | :--- |
| `VIDEO_ANALYTICS_URL` | App → side-car base URL (default `http://video-analytics:8020`). |
| `VA_FRONT_DEVICE` / `VA_BACK_DEVICE` / `VA_CONTENT_DEVICE` | Per-pipeline default device. |
| `VA_RTSP_CODEC`, `VA_RTSP_ENCODER_BACKEND` | Codec / encoder backend. |
| `VA_POSE_*` | Pose-statistics thresholds. |
| `STORAGE_DIR` | Host folder bind-mounted to `/app/storage`. |
| `RENDER_GID` | Render group for `/dev/dri` + `/dev/accel` access. |

---

## Hardware requirements

VA is accelerator-bound. For the default `NPU` device (and VA-API encode/decode)
the host needs an Intel NPU and/or GPU with current drivers, device passthrough
(`/dev/accel`, `/dev/dri`) and the `render` group (handled by
`docker-compose.gpu.yml` + `RENDER_GID`). Set the device to `GPU` to run
without an NPU. See also [va-pipeline-guide.md](./va-pipeline-guide.md) for
architecture.

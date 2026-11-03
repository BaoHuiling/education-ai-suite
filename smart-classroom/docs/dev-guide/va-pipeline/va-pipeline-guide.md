<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Video Analytics (VA) Pipeline — Service Guide

The Video Analytics pipeline is the Smart Classroom component that turns live or
recorded **classroom camera video** into **real-time engagement statistics** and
**annotated video streams**. It detects people, estimates their pose, classifies
posture (sit / stand / raise-hand), re-identifies students across frames, and
publishes per-frame metadata plus aggregated classroom statistics.

On Linux the VA pipeline runs as a dedicated **micro-service** built on
**Intel® Deep Learning Streamer (DL Streamer)** and **OpenVINO™**, and streams
annotated video through a **MediaMTX** relay for in-browser playback.

This document explains *how the service works* and *how to drive it*. For the UI
contract (the app-facing routes), see [§7 Application API](#7-application-api).

---

## 1. Availability and feature switch

| Property | Value |
|---|---|
| Feature id | `video_analytics` (alias `va`) |
| Config switch | `features.video_analytics.enabled` |
| Service (side-car) | `video-analytics` (DL Streamer image) |
| Stream relay (side-car) | `mediamtx` |
| Default device | `NPU` (overridable to `GPU` or `CPU`) |
| Compose profile | `va` |

When `features.video_analytics.enabled` is `false`, the VA routes are not mounted
and the `va` Compose profile is not activated, so neither side-car runs.

---

## 2. What the pipeline produces

For each configured camera the service runs one named pipeline:

| Pipeline | Camera / source | Analysis performed |
|---|---|---|
| `front` | Front-of-class view of students | Pose detection → posture → **ReID + tracking** + two classifiers |
| `back` | Rear/back view of students | Pose detection → posture + one classifier |
| `content` | Board / content camera or content video | Whole-frame **classification only** (no pose) |

Outputs per run:

1. **Per-frame metadata** as JSON-Lines files (one JSON object per line).
2. **Annotated RTSP streams** published to MediaMTX, re-exposed to the browser as
   **WebRTC** (`:8889`) or **HLS** (`:8888`).
3. **Aggregated classroom statistics** (`class_statistics.json`) computed from the
   front posture stream when the session completes.
---

## 3. Architecture

```
HTTP client (app adapter / curl)
   │
   ▼
FastAPI control API  (components/va/control_api.py)
   │   one process-wide VideoAnalyticsPipelineService
   ▼
VideoAnalyticsPipelineService  (va_pipeline_service.py)
   │   builds a GStreamer description (pipeline_builders.py)
   ▼
PipelineRunnerClient  (runner_client.py)     authenticated multiprocessing IPC
   │
   ▼
pipeline_runner.py  (child process)
   │   Gst.parse_launch(description)
   ▼
DL Streamer / GStreamer elements
   │   gvadetect · gvapython · gvaclassify · gvainference · gvatrack
   │   gvametaconvert · gvametapublish · gvawatermark · rtspclientsink
   ├─▶ JSON-Lines metadata files  (outputs/*.txt)
   └─▶ annotated RTSP  ─▶  MediaMTX  ─▶  WebRTC / HLS
```

### Key design points

- **One service instance per process**, one entry per pipeline name. Launching an
  already-running `front`/`back`/`content` is rejected rather than duplicated.
- **Each pipeline runs in a child process.** The parent and child communicate over
  authenticated `multiprocessing` IPC (not just exit codes), exchanging
  `playing` / `warning` / `error` / `eos` events and a cooperative `stop`.
- **Resilience:** the parent monitors each child, **retries up to 10 times** on
  unexpected exit, applies a **stall watchdog** (kills a process whose metadata
  stops growing for ~60 s) and a startup grace window (~120 s). File sources can
  resume past a bad segment. After retries are exhausted the pipeline is `failed`.
- **Posture without a native plugin.** Unlike the Windows build (which loads a
  native `gvasmartclassroom.dll`), the Linux service implements posture in Python
  via `gvapython` → `gvapython/posture_detect.py` (`PostureDetect`). It reads the
  YOLO pose keypoint tensor and attaches a custom `posture` tensor to each
  detection. ROI filtering (`roi_filter.py`) and statistics (`pose_stats.py`) are
  likewise pure-Python post-processing over the JSON-Lines records.

---

## 4. The three pipelines in detail

### 4.1 `front`
```
source → gvafpscounter → videorate(30fps) → gvadetect(YOLO pose)
       → gvapython(PostureDetect) → tee
   ├─ classify branch   → videorate(1fps) → gvaclassify(resnet18)
   │                    → gvametaconvert → gvametapublish  front_resnet18.txt
   ├─ track/video branch→ gvainference(ReID) → gvatrack(deep-sort)
   │                    → gvametaconvert → gvametapublish  front_posture.txt
   │                    → gvawatermark → RTSP sink (or fakesink)
   └─ classify branch   → videorate(1fps) → gvaclassify(mobilenetv2)
                        → gvametaconvert → gvametapublish  front_mobilenetv2.txt
```
The ReID/tracking branch is **not** frame-decimated — Deep SORT needs continuous
frames to keep IDs stable. `front_posture.txt` is the principal statistics source.

### 4.2 `back`
```
source → gvafpscounter → videorate(30fps) → gvadetect(YOLO pose)
       → gvapython(PostureDetect) → gvawatermark
       → gvametaconvert → gvametapublish  back_posture.txt → tee
   ├─ video branch    → RTSP sink (or fakesink)
   └─ classify branch → videorate(1fps) → gvaclassify(resnet18)
                      → gvametaconvert → gvametapublish  back_resnet18.txt
```

### 4.3 `content`
```
source → videorate(1fps) → gvaclassify(resnet18) → gvafpscounter
       → gvametaconvert → gvametapublish  content_results.txt
       → gvawatermark → RTSP sink (or fakesink)
```
Whole-frame classification only; no pose, ROI, tracking, or `gvapython`.

### 4.4 Sources
- **Local file** → `filesrc ! decodebin3` (validated up front with
  `gst-discoverer-1.0`; a missing/undecodable file fails the launch).
- **RTSP URL** → H.264/H.265 decode over **TCP** (`VA_RTSP_CODEC`). UDP is avoided
  because fragmented FU-A packets were lost/corrupted across the Docker bridge.

### 4.5 Stream output
`_get_rtsp_sink_elements()` publishes via `rtspclientsink`, with two encoder
backends:
- `va` (default) — VA-API: `vapostproc` + `vah264enc`/`vah265enc` (GPU/NPU).
- `cpu` — software: `videoconvert` + `openh264enc`.

Streams are published to `rtsp://mediamtx:8554/{front,back,content}_stream`.
When `output_stream=false`, the video branch terminates in `fakesink`.

### 4.6 Device selection

Each `gvadetect`/`gvaclassify`/`gvainference` element takes `device=` (`CPU`,
`GPU`, or `NPU`) plus a `pre-process-backend` that must match the kind of
memory the frame arrives in:

- **GPU/NPU** (default): `pre-process-backend` is left unset. DL Streamer
  auto-selects `va`, which maps the VA-memory frames (`VASurface`/`VAMemory`)
  that `vah264dec`/`vah265dec` hand it zero-copy — no CPU-side copy needed.
- **CPU**: the `va` backend has no meaning for a CPU inference target, and
  the CPU plugin can't map VA-memory anyway, so `_get_source_elements`
  appends an explicit `video/x-raw` caps filter after the hardware decoder
  (forcing it to download frames to plain system memory) and
  `_get_preprocess_backend_elements` sets `pre-process-backend=opencv` on
  every inference element, which does color-conversion/resize on that
  system memory with OpenCV instead.

This only changes the *inference* path — RTSP hardware decode/encode
(`vah264dec`/`vah265dec`, `vapostproc`+`vah264enc`) still run on the GPU
regardless of `device`, independently controlled by `VA_RTSP_ENCODER_BACKEND`
(§10). A box with no `/dev/dri` at all needs `VA_RTSP_ENCODER_BACKEND=cpu`
too.

The Windows build (`Windows/components/va/va_pipeline_service.py`) mirrors
this exactly, just swapping VA-API's vocabulary for Media Foundation's:
`pre-process-backend` is explicitly `d3d11` for GPU/NPU and `opencv` for CPU,
and `d3d12download` (after `d3d12h264dec`/`d3d12h265dec`/`decodebin3`) is
asked for plain `video/x-raw` instead of `video/x-raw(memory:D3D11Memory)`
when `device` is CPU. RTSP encode (`d3d11convert`+`mfh264enc`) is likewise
unconditional on the GPU.

---

## 5. Models

Runtime OpenVINO IR models, expected under `models/va/`:

| Role | File | Source |
|---|---|---|
| Front pose | `<front_pose_model>.xml/.bin` (default `yolov8m-pose`) | Ultralytics → OpenVINO |
| Back pose | `<back_pose_model>.xml/.bin` (default `yolov8s-pose`) | Ultralytics → OpenVINO |
| Classifier A | `resnet18.xml/.bin` | torchvision → OpenVINO |
| Classifier B | `mobilenetv2.xml/.bin` | torchvision → OpenVINO |
| Re-identification | `person-reidentification-retail-0288.xml/.bin` | Open Model Zoo |

Supported pose families: YOLOv8 / YOLO11 / YOLO26 (`*m-pose`, `*s-pose`).

**Export is a dev-time / provisioning step, not part of the runtime image.** The
helpers `utils/convert_yolo_models.py` and `utils/convert_classification_models.py`:
- download the source weights,
- export to OpenVINO IR (FP16, static reshape — e.g. pose `[1,3,384,640]`,
  classifiers `[1,3,224,224]`),
- apply NNCF INT8 quantization,
- embed DL Streamer metadata (model type, labels
  `sit stand sit_raise_up stand_raise_up`),
- and verify the IR on NPU via `utils/model_verification.py` (devices without NPU
  are **skipped with a warning**, not failed).

The export tooling lives in `requirements-models.txt` and is intentionally **not**
installed into the service image (`requirements.txt` only).

---

## 6. Service (control) API

The service exposes a thin FastAPI surface (`components/va/control_api.py`) on
port `8000` inside the container. All metadata responses are
`application/x-ndjson` where noted.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/pipelines/{name}/launch` | Start a `front`/`back`/`content` pipeline |
| `POST` | `/pipelines/{name}/stop?timeout=10` | Gracefully stop a pipeline |
| `GET` | `/pipelines` | Status of all registered pipelines |
| `GET` | `/pipelines/{name}` | Status of one pipeline |
| `GET` | `/pipelines/status?check_interval=2` | **NDJSON** live status stream |
| `GET` | `/class-statistics?front_posture_file=…&poll_interval=5` | **NDJSON** live statistics stream |

Unknown pipeline names return `404`; service `ValueError`/`RuntimeError` surface as
`400`.

### `POST /pipelines/{name}/launch`

Body (`LaunchRequest`):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `source` | string | *required* | File path or RTSP URL |
| `device` | string? | `null` | `CPU` / `GPU` / `NPU`; `null` → per-pipeline default |
| `output_dir` | string | `outputs` | Metadata/log/output directory |
| `output_rtsp` | string | `rtsp://127.0.0.1:8554` | RTSP relay base (use `rtsp://mediamtx:8554` in Compose) |
| `output_stream` | bool | `true` | Publish annotated video, else `fakesink` |
| `threshold` | float | `0.5` | YOLO detection confidence |
| `record` | bool | `false` | Record the **source** RTSP via FFmpeg |

Response: `{"launched": true}`.

### `GET /pipelines/{name}` — status object
`name`, `running`, `pid`, `return_code`, `final_status`
(`null`/`eos`/`failed`/`stopped`), `cpu_percent`, `memory_mb`, `uptime_seconds`,
`log_file`, `output_files[]`.

---

## 7. Application API

The Smart Classroom app surfaces VA through session-aware routes (identical to the
Windows build so the UI is unchanged). Every route takes an `x-session-id` header.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/start-video-analytics-pipeline` | Launch one or more pipelines for the session |
| `POST` | `/stop-video-analytics-pipeline` | Stop one or more pipelines |
| `GET` | `/monitor-video-analytics-pipeline` | NDJSON lifecycle/status stream |
| `GET` | `/class-statistics` | NDJSON classroom statistics (every 5 s) |
| `GET` | `/check-recorded-videos` | Which `front`/`back`/`board` recordings exist |
| `GET` | `/recorded-video/{videoType}` | Serve a recorded MP4 |

`start` request items are `{pipeline_name, source, output_stream}`. On success each
result carries `status`, `pipeline_name`, `session_id`, and — when
`output_stream` is true — a browser-playable `stream_url`
(`{webrtc_base_url}/{name}_stream` or the HLS equivalent) and `stream_protocol`.

---

## 8. Classroom statistics

When a session's pipelines finish, the app reads `front_posture.txt` and produces
`class_statistics.json`. The live `/class-statistics` stream emits the same shape:

```json
{
  "student_count": 4,
  "stand_count": 2,
  "raise_up_count": 1,
  "stand_reid": [ { "student_id": 1, "count": 2 } ]
}
```

The incremental tracker (`pose_stats.py`) folds the posture stream into these
counts, handling: identified vs unidentified people, `stand`/`stand_raise_up`
transitions, `sit_raise_up`/`stand_raise_up` raise-hand events, absence/stale
tracking, minimum transition/stand-frame thresholds, and a student-count window.
All thresholds are tunable (see `va_pipeline.pose_statistics.*` and `VA_POSE_*`).

---

## 9. Output artifacts

Written under the session's VA output directory (e.g. `…/raw/va/`):

| File | Format | Pipeline | Contents |
|---|---|---|---|
| `front_posture.txt` | JSON-Lines | front | Pose labels, detections, ReID/posture events (statistics source) |
| `front_resnet18.txt` | JSON-Lines | front | ResNet-18 classification |
| `front_mobilenetv2.txt` | JSON-Lines | front | MobileNetV2 classification |
| `back_posture.txt` | JSON-Lines | back | Back pose/posture metadata |
| `back_resnet18.txt` | JSON-Lines | back | Back classification |
| `content_results.txt` | JSON-Lines | content | Content-camera classification |
| `class_statistics.json` | JSON | session | Aggregated statistics (+ `duration_sec`) |
| `logs/<pipeline>_<unixtime>.log` | text | service | Per-run runner/pipeline log |
| `<pipeline>.mp4` | MP4 | optional | Recording when `record=true` |

---

## 10. Configuration

`config.yaml`:

```yaml
features:
  video_analytics: { enabled: false }   # opt-in

models:
  va:
    models_base_path: "models"
    device: NPU                 # NPU | GPU | CPU
    front_pose_model: yolov8m-pose   # yolov8m-pose | yolo11m-pose | yolo26m-pose
    back_pose_model: yolov8s-pose    # yolov8s-pose | yolo11s-pose | yolo26s-pose
    threshold: 0.5

va_pipeline:
  endpoint: http://video-analytics:8020    # resolved from VIDEO_ANALYTICS_URL first
  rtsp_codec: h264               # h264 | h265
  output_rtsp_url: rtsp://mediamtx:8554
  completion_timeout_sec: 3600
  hls_base_url: http://localhost:8888      # browser-facing
  webrtc_base_url: http://localhost:8889   # browser-facing
  stream_protocol: webrtc        # webrtc | hls
  pose_statistics:
    min_frames_for_transition: 3
    min_frames_for_transition_unid: 15
    absence_threshold: 90
    min_stand_frames: 10
    center_dist_threshold: 0.1
    unidentified_max: 50
    stale_unidentified_threshold: 30
```

### Environment variables (service side-car)

| Variable | Default | Purpose |
|---|---|---|
| `VA_RTSP_CODEC` | `h264` | RTSP decode/encode codec |
| `VA_RTSP_ENCODER_BACKEND` | `va` | `va` (VA-API) or `cpu` |
| `VA_FRONT_DEVICE` / `VA_BACK_DEVICE` / `VA_CONTENT_DEVICE` | `NPU` | Per-pipeline default device |
| `VA_POSE_MIN_FRAMES_FOR_TRANSITION` | `3` | Raise-hand confirm frames (identified) |
| `VA_POSE_MIN_FRAMES_FOR_TRANSITION_UNID` | `15` | Raise-hand confirm frames (unidentified) |
| `VA_POSE_ABSENCE_THRESHOLD` | `90` | Forget a student after N unseen frames |
| `VA_POSE_MIN_STAND_FRAMES` | `10` | Frames before counting a stand-up |
| `VA_POSE_CENTER_DIST_THRESHOLD` | `0.1` | Box proximity to treat as same person |
| `VA_POSE_UNIDENTIFIED_MAX` | `50` | Max unidentified tracked |
| `VA_POSE_STALE_UNIDENTIFIED_THRESHOLD` | `30` | Drop stale unidentified after N frames |
| `VA_POSE_STUDENT_COUNT_WINDOW_FRAMES` | `150` | Student-count smoothing window |
| `VA_CONFIG_PATH` | repo `config.yaml` | Override config path |

The app-side endpoint is injected via `VIDEO_ANALYTICS_URL`
(e.g. `http://video-analytics:8020`), mirroring `AUDIO_ANALYZER_URL` /
`SERVING_BASE_URL`.

---

## 11. Hardware & runtime requirements

VA is accelerator-bound. For the default `NPU` device (and VA-API encode/decode):

- Intel® NPU and/or GPU with current drivers.
- Device passthrough: `/dev/dri` (GPU/VA-API) and `/dev/accel` (NPU).
- The host `render` group GID (`RENDER_GID`) added to the container.
- DL Streamer elements registered and `gstgva` Python bindings importable.
- MediaMTX reachable from the service, with inter-container RTSP traffic bypassing
  any corporate proxy (`no_proxy` must include `mediamtx`, `video-analytics`).

Without NPU hardware, set the device to `GPU`. Model-export NPU
verification is skipped (not failed) when no NPU is enumerated.

Setting the device to `CPU` (see §4.6) needs no `/dev/dri`/`/dev/accel`
passthrough or `render` group membership for *inference* itself, only an
OpenVINO CPU plugin (bundled with DL Streamer) — useful for offloading model
inference to the CPU while still using an available iGPU for video
decode/encode. RTSP sources still decode on the GPU (`vah264dec`/`vah265dec`,
§4.4) regardless of `device`, so `/dev/dri` is still required whenever the
source is RTSP; a file source decodes in software via `decodebin3` either way.
For a box with no GPU at all, use a file source and set
`VA_RTSP_ENCODER_BACKEND=cpu` so the RTSP *output* encode also runs in
software (§10).

Validate the environment with `scripts/va-dev-env-check.sh` (checks DL Streamer
elements, OpenVINO GPU/NPU compile+inference, `gstgva` import, `/dev/dri`, and the
render group; skips NPU checks if `/dev/accel` is absent).

---

## 12. Streaming & playback

| Port | Protocol | Use |
|---|---|---|
| `8554` | RTSP | Service publishes annotated streams into MediaMTX |
| `8889` | WebRTC | Low-latency browser playback (`{webrtc_base_url}/{name}_stream`) |
| `8888` | HLS | Compatibility playback (`{hls_base_url}/{name}_stream`) |

Choose the player protocol with `va_pipeline.stream_protocol`. The service is a
*publisher*; browsers read the relayed stream from MediaMTX.

---

## 13. Related

- `board_ocr` is a **separate** feature. The VA `content` pipeline may provide the
  content video it works from, but VA performs no OCR and does not embed board_ocr.
- Report integration: see
  [report-generator-api.md](../report-generator/report-generator-api.md) — VA
  statistics reach the report via `class_statistics.json`.

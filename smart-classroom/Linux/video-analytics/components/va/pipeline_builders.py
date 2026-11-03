"""Pipeline element assembly for the front/back/content cameras."""

from pathlib import Path
from typing import List

_POSTURE_DETECT_MODULE = (
    Path(__file__).resolve().parent / "gvapython" / "posture_detect.py"
).as_posix()


def build_pipeline_front(service, source: str, options, input_type: str) -> List[str]:
    """Build front camera pipeline (Pipeline 1)"""
    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline = [
        *service._get_source_elements(source, input_type, options),
        "gvafpscounter",
        "!",
        *service._get_input_framerate_cap_elements(),
        # YOLO pose detection
        "gvadetect",
        f"model={service._get_model_path('front-pose')}",
        f"device={options.device}",
        *service._get_preprocess_backend_elements(options),
        "batch-size=1",
        "model-instance-id=yolo-front-0",
        f"threshold={options.threshold}",
        "inference-region=full-frame",
        "!",
        "gvapython",
        f"module={_POSTURE_DETECT_MODULE}",
        "class=PostureDetect",
        "!",
        "tee",
        "name=t",
        # Branch 1: ResNet18 classification
        "t.",
        "!",
        "queue",
        "!",
        *service._get_classify_decimation_elements(),
        "gvaclassify",
        f"model={service._get_model_path('resnet18')}",
        f"device={options.device}",
        *service._get_preprocess_backend_elements(options),
        "batch-size=1",
        "inference-region=roi-list",
        "model-instance-id=resnet18-front-0",
        "!",
        "gvametaconvert",
        "add-tensor-data=true",
        "!",
        "gvametapublish",
        f"file-path={output_dir.as_posix()}/front_resnet18.txt",
        "file-format=json-lines",
        "!",
        "fakesink",
        "async=false",
        "sync=false",
        # Branch 2: ReID (gvainference+gvatrack) and RTSP output. Not
        # decimated -- deep-sort tracking needs continuous frame-to-frame
        # input to keep IDs stable.
        "t.",
        "!",
        "queue",
        "!",
        "gvainference",
        f"model={service._get_model_path('reid')}",
        f"device={options.device}",
        *service._get_preprocess_backend_elements(options),
        "batch-size=1",
        "inference-region=roi-list",
        "model-instance-id=reid-front-0",
        "!",
        "gvatrack",
        "tracking-type=deep-sort",
        "!",
        "gvametaconvert",
        "add-tensor-data=true",
        "!",
        "gvametapublish",
        f"file-path={output_dir.as_posix()}/front_posture.txt",
        "file-format=json-lines",
        "!",
        "gvawatermark",
        "!",
        # Not on a decimated branch, so this sink runs at the capped source rate.
        *service._get_video_sink_elements(
            options, "front_stream", service.max_input_framerate
        ),
        # Branch 3: MobileNetV2 classification
        "t.",
        "!",
        "queue",
        "!",
        *service._get_classify_decimation_elements(),
        "gvaclassify",
        f"model={service._get_model_path('mobilenetv2')}",
        f"device={options.device}",
        *service._get_preprocess_backend_elements(options),
        "batch-size=1",
        "inference-region=roi-list",
        "model-instance-id=mobilenetv2-front-0",
        "!",
        "gvametaconvert",
        "add-tensor-data=true",
        "!",
        "gvametapublish",
        f"file-path={output_dir.as_posix()}/front_mobilenetv2.txt",
        "file-format=json-lines",
        "!",
        "fakesink",
        "async=false",
        "sync=false",
    ]
    return pipeline


def build_pipeline_back(service, source: str, options, input_type: str) -> List[str]:
    """Build back camera pipeline (Pipeline 2)"""
    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    return [
        *service._get_source_elements(source, input_type, options),
        "gvafpscounter",
        "!",
        *service._get_input_framerate_cap_elements(),
        # YOLO pose detection
        "gvadetect",
        f"model={service._get_model_path('back-pose')}",
        f"device={options.device}",
        *service._get_preprocess_backend_elements(options),
        "batch-size=1",
        "model-instance-id=yolo-back-0",
        f"threshold={options.threshold}",
        "inference-region=full-frame",
        "!",
        "gvapython",
        f"module={_POSTURE_DETECT_MODULE}",
        "class=PostureDetect",
        "!",
        "gvawatermark",
        "!",
        "gvametaconvert",
        "add-tensor-data=true",
        "!",
        "gvametapublish",
        f"file-path={output_dir.as_posix()}/back_posture.txt",
        "file-format=json-lines",
        "!",
        "tee",
        "name=t",
        # Branch 1: video output, kept at the source framerate. Split off
        # ahead of the classification decimation so the stream is not
        # thinned too; gvawatermark already ran, and it only draws the pose
        # detections.
        "t.",
        "!",
        "queue",
        "!",
        *service._get_video_sink_elements(
            options, "back_stream", service.max_input_framerate
        ),
        # Branch 2: ResNet18 classification
        "t.",
        "!",
        "queue",
        "!",
        *service._get_classify_decimation_elements(),
        "gvaclassify",
        f"model={service._get_model_path('resnet18')}",
        f"device={options.device}",
        *service._get_preprocess_backend_elements(options),
        "batch-size=1",
        "inference-region=roi-list",
        "model-instance-id=resnet18-back-0",
        "!",
        "gvametaconvert",
        "add-tensor-data=true",
        "!",
        "gvametapublish",
        f"file-path={output_dir.as_posix()}/back_resnet18.txt",
        "file-format=json-lines",
        "!",
        "fakesink",
        "async=false",
        "sync=false",
    ]


def build_pipeline_content(service, source: str, options, input_type: str) -> List[str]:
    """Build content/file pipeline (Pipeline 3)

    No custom elements here (no pose detect, no ROIs) -- whole-frame
    classification only. Device stays independently configurable, not
    pinned to NPU like front/back (see VA-10).
    """
    output_dir = Path(options.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    return [
        *service._get_source_elements(source, input_type, options),
        # Unlike front/back this rate applies to the whole pipeline, video
        # output included, so the sink below is told the same framerate.
        *service._get_classify_decimation_elements(),
        "gvaclassify",
        f"model={service._get_model_path('resnet18')}",
        f"device={options.device}",
        *service._get_preprocess_backend_elements(options),
        "batch-size=1",
        "inference-region=full-frame",
        "model-instance-id=resnet18-content-0",
        "!",
        "gvafpscounter",
        "!",
        "gvametaconvert",
        "add-tensor-data=true",
        "!",
        "gvametapublish",
        f"file-path={output_dir.as_posix()}/content_results.txt",
        "file-format=json-lines",
        "!",
        "gvawatermark",
        "!",
        *service._get_video_sink_elements(
            options, "content_stream", service.classify_framerate
        ),
    ]

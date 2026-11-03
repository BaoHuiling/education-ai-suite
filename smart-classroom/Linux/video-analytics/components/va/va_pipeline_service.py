import subprocess
import os
import time
import logging
from pathlib import Path
from typing import Optional, Dict, List, Generator
import psutil
from dataclasses import dataclass, replace
from enum import Enum
import atexit
import json
import threading
from utils.config_loader import config
from utils.rtsp_recorder import (
    start_rtsp_recording,
    stop_rtsp_recording,
)
from utils.gstreamer_env import GST_SUBPROCESS_TIMEOUT, ensure_gst_registry
from components.va.runner_client import PipelineRunnerClient
from components.va.pose_stats import PoseStatsTracker
from components.va.pipeline_builders import (
    build_pipeline_front,
    build_pipeline_back,
    build_pipeline_content,
)

MAX_INPUT_FRAMERATE = "30/1"
CLASSIFY_FRAMERATE = "1/1"
RTSP_KEYFRAME_INTERVAL_SEC = 0.5


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default

class PipelineName(Enum):
    """Enumeration of pipeline names"""

    FRONT = "front"  # Pipeline 1
    BACK = "back"  # Pipeline 2
    CONTENT = "content"  # Pipeline 3

@dataclass
class PipelineOptions:
    """Minimal configuration options for pipelines"""
    device: Optional[str] = None
    output_dir: str = "outputs"  # Directory for metadata output files
    output_rtsp: str = "rtsp://127.0.0.1:8554"  # RTSP output URL
    output_stream: bool = True  # Push video to RTSP; False = discard to fakesink
    threshold: float = 0.5  # Detection threshold for YOLO
    record: bool = False


class VideoAnalyticsPipelineService:
    """Service to manage video analytics pipelines"""

    def __init__(self):
        """
        Initialize VideoAnalyticsPipelineService
        """
        self.logger = logging.getLogger(self.__class__.__name__)

        # Set model paths
        self.model_base_dir = Path(config.models.va.models_base_path).resolve() / "va"
        self.models = {
            "front-pose": f"{config.models.va.front_pose_model}.xml",
            "back-pose": f"{config.models.va.back_pose_model}.xml",
            "resnet18": "resnet18.xml",
            "mobilenetv2": "mobilenetv2.xml",
            "reid": "person-reidentification-retail-0288.xml",
        }

        # Active pipelines
        self.pipelines: Dict[str, subprocess.Popen] = {}

        # Pipeline log files & handles
        self.pipeline_logs: Dict[str, Path] = {}
        self.pipeline_log_handles: Dict[str, object] = {}

        # Pipeline output files
        self.pipeline_output_files: Dict[str, List[Path]] = {}

        # Pipeline monitoring threads
        self.monitor_threads: Dict[str, threading.Thread] = {}
        self.monitor_stop_flags: Dict[str, threading.Event] = {}

        # Pipeline launch parameters for restart
        self.pipeline_params: Dict[str, Dict] = {}

        # Pipeline retry counts
        self.pipeline_retry_counts: Dict[str, int] = {}
        self.max_retries = 10

        # How long to wait for a pipeline to report PLAYING on its bus before
        # calling the launch failed.
        self.pipeline_ready_timeout = 60.0

        # "eos" (normal), "failed" (gave up after max retries), or "stopped" (manual stop).
        self.pipeline_final_status: Dict[str, str] = {}

        # Pipeline error events for status reporting (consumed by monitor_pipeline_status)
        self.pipeline_errors: Dict[str, List[str]] = {}

        # Callback fired once when all pipelines have finished (EOS or stopped).
        # Signature: on_all_pipelines_done(session_id: str) -> None
        self.on_all_pipelines_done = None
        self._reports_generated = False   # guard: fire at most once per service instance
        self._any_pipeline_ran  = False   # guard: don't fire before any pipeline was launched

        ps_env_prefix = "VA_POSE_"
        self.min_frames_for_transition = _env_int(ps_env_prefix + "MIN_FRAMES_FOR_TRANSITION", 3)
        self.min_frames_for_transition_unid = _env_int(ps_env_prefix + "MIN_FRAMES_FOR_TRANSITION_UNID", 15)
        self.absence_threshold = _env_int(ps_env_prefix + "ABSENCE_THRESHOLD", 90)
        self.min_stand_frames = _env_int(ps_env_prefix + "MIN_STAND_FRAMES", 10)
        self.center_dist_threshold = _env_float(ps_env_prefix + "CENTER_DIST_THRESHOLD", 0.1)
        self.unidentified_max = _env_int(ps_env_prefix + "UNIDENTIFIED_MAX", 50)
        self.stale_unidentified_threshold = _env_int(ps_env_prefix + "STALE_UNIDENTIFIED_THRESHOLD", 30)
        self.student_count_window_frames = _env_int(ps_env_prefix + "STUDENT_COUNT_WINDOW_FRAMES", 150)

        # Engagement-statistics parser
        self._pose_stats_tracker = PoseStatsTracker(
            min_frames_for_transition=self.min_frames_for_transition,
            min_frames_for_transition_unid=self.min_frames_for_transition_unid,
            absence_threshold=self.absence_threshold,
            min_stand_frames=self.min_stand_frames,
            center_dist_threshold=self.center_dist_threshold,
            unidentified_max=self.unidentified_max,
            stale_unidentified_threshold=self.stale_unidentified_threshold,
            student_count_window_frames=self.student_count_window_frames,
        )

        # Upper bound on the framerate the pipeline runs inference at. Protects against high-framerate cameras.
        self.max_input_framerate = MAX_INPUT_FRAMERATE
        # Framerate the classification branches run at.
        self.classify_framerate = CLASSIFY_FRAMERATE

        # Which encoder chain _get_rtsp_sink_elements uses: "va" (vapostproc +
        # vah264enc, needs /dev/dri) or "cpu" (videoconvert + openh264enc,
        # software fallback for a box with no VA-API device).
        self.rtsp_encoder_backend = os.environ.get("VA_RTSP_ENCODER_BACKEND", "va")
        # "h264" or "h265" -- selects the RTSP source decode chain in
        # _get_source_elements (vah264dec vs vah265dec).
        self.rtsp_codec = os.environ.get("VA_RTSP_CODEC", "h264")

        # Application-level default device per pipeline (VA-10). Configurable
        # per deployment via env var; a caller can still override per launch
        # by setting PipelineOptions.device explicitly (see launch_pipeline).
        self.default_device = {
            PipelineName.FRONT.value: os.environ.get("VA_FRONT_DEVICE", "NPU"),
            PipelineName.BACK.value: os.environ.get("VA_BACK_DEVICE", "NPU"),
            PipelineName.CONTENT.value: os.environ.get("VA_CONTENT_DEVICE", "NPU"),
        }

        # Settle the GStreamer environment before the first GStreamer process
        # runs, so every process this service spawns shares one registry cache.
        self._setup_environment()

        # Register cleanup handler
        atexit.register(self._cleanup)

    def _setup_environment(self):
        """Setup GStreamer environment variables. Idempotent.
        Runner children inherit this environment, so DL Streamer only has to be
        located here.
        """
        ensure_gst_registry()
        os.environ["GST_DEBUG"] = (
            "GVA_common:2,gvapython:4"
        )

    def _get_model_path(self, model_key: str) -> str:
        """Get full path to model"""
        return (self.model_base_dir / self.models[model_key]).as_posix()

    def _validate_file_with_discoverer(self, file_path: str) -> Optional[str]:
        """Validate a file source using gst-discoverer-1.0

        Args:
            file_path: Path to the file to validate

        Returns:
            None if the file is valid, error message string if invalid
        """
        try:
            result = subprocess.run(
                ["gst-discoverer-1.0", file_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GST_SUBPROCESS_TIMEOUT,
            )
            combined_output = result.stdout + result.stderr
            if "An error was encountered while discovering the file" in combined_output:
                self.logger.error(
                    f"File validation failed for '{file_path}': {combined_output}"
                )
                return combined_output.strip()
            return None
        except FileNotFoundError:
            self.logger.error("gst-discoverer-1.0 not found")
            return "gst-discoverer-1.0 not found"
        except subprocess.TimeoutExpired:
            self.logger.error(f"File validation timed out for '{file_path}'")
            return "File validation timed out"
        except Exception as e:
            self.logger.error(f"File validation error: {e}")
            return f"File validation error: {e}"

    def _get_source_elements(
        self,
        source: str,
        input_type: str,
        options: Optional["PipelineOptions"] = None,
    ) -> List[str]:
        """Get source elements based on input type

        Args:
            options: resolved launch options. `vah264dec`/`vah265dec` decode
                on the GPU and hand frames downstream as VA-memory
                (VASurface/VAMemory), which only GPU/NPU inference can map
                zero-copy. When options.device is CPU, append explicit
                `video/x-raw` caps so the decoder downloads frames to plain
                system memory instead -- the only kind the 'opencv'
                pre-process-backend (see _get_preprocess_backend_elements)
                can read.
        """
        system_memory_caps = (
            ["video/x-raw", "!"] if options is not None and options.device == "CPU" else []
        )
        if input_type == "rtsp" and self.rtsp_codec == "h264":
            return [
                "rtspsrc",
                f"location={source}",
                "protocols=tcp",
                "!",
                "rtph264depay",
                "wait-for-keyframe=true",
                "!",
                "h264parse",
                "!",
                "vah264dec",
                "!",
                *system_memory_caps,
            ]
        elif input_type == "rtsp" and self.rtsp_codec == "h265":
            return [
                "rtspsrc",
                f"location={source}",
                "protocols=tcp",
                "!",
                "rtph265depay",
                "wait-for-keyframe=true",
                "!",
                "h265parse",
                "!",
                "vah265dec",
                "!",
                *system_memory_caps,
            ]
        elif input_type == "file":
            return [
                "filesrc",
                f"location={Path(source).as_posix()}",
                "!",
                "decodebin3",
                "!",
            ]
        else:
            raise ValueError(f"Unknown input type: {input_type}")

    def _get_preprocess_backend_elements(self, options: "PipelineOptions") -> List[str]:
        """Get the gva* 'pre-process-backend' override for options.device.

        Left on auto-selection for GPU/NPU: DL Streamer picks 'va' on its own
        from the VA-memory frames _get_source_elements hands it. CPU has no
        VA-API inference path, so pre-processing (color conversion/resize)
        needs the explicit 'opencv' backend, which only accepts system
        memory -- hence the video/x-raw caps _get_source_elements adds for
        this same device.
        """
        if options.device == "CPU":
            return ["pre-process-backend=opencv"]
        return []

    @staticmethod
    def _framerate_to_fps(framerate: Optional[str]) -> float:
        """Convert a GStreamer 'num/den' framerate to fps. Falls back to 30."""
        if not framerate:
            return 30.0
        num, _, den = str(framerate).partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (TypeError, ValueError, ZeroDivisionError):
            return 30.0
        return fps if fps > 0 else 30.0

    def _gop_size(self, sink_framerate: Optional[str]) -> int:
        """Keyframe interval in frames, for a pipeline running at sink_framerate.

        The encoder's gop-size counts *pictures*, not seconds. Deriving it
        from the framerate keeps the keyframe cadence constant in wall-clock
        time.
        """
        fps = self._framerate_to_fps(sink_framerate)
        return max(1, round(fps * RTSP_KEYFRAME_INTERVAL_SEC))

    def _get_rtsp_sink_elements(
        self, rtsp_url: str, pipeline_name: str, sink_framerate: Optional[str] = None
    ) -> List[str]:
        """Get RTSP sink elements for pushing to RTSP server."""
        gop_size = self._gop_size(sink_framerate)

        if self.rtsp_encoder_backend == "cpu":
            encode_elements = [
                "videoconvert",
                "!",
                "openh264enc",
                "bitrate=3000000",
                f"gop-size={gop_size}",
                "rate-control=bitrate",
                "complexity=low",
            ]
        else:
            encode_elements = [
                "vapostproc",
                "!",
                "vah264enc",
                "bitrate=3000",
                f"key-int-max={gop_size}",
                "b-frames=0",
                "rate-control=cbr",
            ]

        return [
            *encode_elements,
            "!",
            "h264parse",
            "!",
            "queue",
            "!",
            "rtspclientsink",
            f"location={rtsp_url}/{pipeline_name}",
            # tcp, not udp (Windows' original): confirmed empirically (VA-14)
            # that udp drops/corrupts fragmented H.264 FU-A packets over a
            # Docker bridge network ("invalid FU-A packet" on the MediaMTX
            # side) -- tcp is lossless and the latency cost of one extra
            # internal container hop is negligible next to that.
            "protocols=tcp",
        ]

    def _get_video_sink_elements(
        self,
        options: PipelineOptions,
        stream_name: str,
        sink_framerate: Optional[str] = None,
    ) -> List[str]:
        """Get video sink elements: RTSP sink, or a discarding fakesink when
        streaming is disabled (options.output_stream=False)

        Args:
            sink_framerate: the framerate actually reaching this sink, as
                'num/den'. Sets the keyframe interval; see _gop_size.
        """
        if not options.output_stream:
            return ["fakesink", "async=false", "sync=false"]
        return self._get_rtsp_sink_elements(
            options.output_rtsp, stream_name, sink_framerate
        )

    def _get_input_framerate_cap_elements(self) -> List[str]:
        """Get elements capping the framerate ahead of gvadetect.
        It exists so a 50/60fps camera cannot push the pose model past the rate the
        NPU can serve.
        """
        if not self.max_input_framerate:
            return []
        return [
            "videorate",
            "drop-only=true",
            "!",
            f"video/x-raw,framerate=[0/1,{self.max_input_framerate}]",
            "!",
        ]

    def _get_classify_decimation_elements(self) -> List[str]:
        """Get elements dropping a classification branch to classify_framerate."""
        if not self.classify_framerate:
            return []
        return [
            "videorate",
            "drop-only=true",
            "!",
            f"video/x-raw,framerate={self.classify_framerate}",
            "!",
        ]

    @staticmethod
    def _join_pipeline_description(elements: List[str]) -> str:
        """Join argv-style pipeline tokens into a gst-parse description string.

        Argv tokens are space-safe; a parse-launch description is not. Quote
        the value half of any token carrying whitespace.
        """
        parts = []
        for token in elements:
            if not any(char.isspace() for char in token):
                parts.append(token)
            elif "=" in token:
                key, value = token.split("=", 1)
                parts.append(f'{key}="{value}"')
            else:
                parts.append(f'"{token}"')
        return " ".join(parts)

    def _monitor_pipeline(self, pipeline_name: str):
        """
        Monitor pipeline process and restart if it exits unexpectedly

        Args:
            pipeline_name: Name of the pipeline to monitor
        """
        stop_flag = self.monitor_stop_flags[pipeline_name]

        while not stop_flag.is_set():
            # Check if pipeline process is still running
            if pipeline_name not in self.pipelines:
                break

            process = self.pipelines[pipeline_name]

            # Check process status
            if process.poll() is not None:
                # Process has exited. The runner reports EOS as a typed event.
                log_file = self.pipeline_logs.get(pipeline_name)
                normal_exit = process.exited_normally()
                self.logger.info(
                    f"[VA][monitor] pipeline '{pipeline_name}' exited rc={process.returncode} "
                    f"normal_exit={normal_exit} final_event={process.final_event} log={log_file}"
                )

                if normal_exit:
                    # Normal exit with EOS
                    self.logger.info(
                        f"Pipeline '{pipeline_name}' exited normally (EOS received)"
                    )
                    self.logger.info(
                        f"[VA][monitor] marking '{pipeline_name}' eos; firing done callback"
                    )
                    self._finalize_pipeline(pipeline_name, "eos")
                    break
                else:
                    # Unexpected exit — record error for status reporting
                    error_detail = (
                        process.error_text()
                        or f"Pipeline exited with code {process.returncode}"
                    )
                    if pipeline_name not in self.pipeline_errors:
                        self.pipeline_errors[pipeline_name] = []
                    self.pipeline_errors[pipeline_name].append(error_detail)
                    self.logger.warning(
                        f"Pipeline '{pipeline_name}' exited unexpectedly: {error_detail}"
                    )

                    retry_count = self.pipeline_retry_counts.get(pipeline_name, 0)

                    if retry_count < self.max_retries:
                        self.logger.warning(
                            f"Pipeline '{pipeline_name}' exited unexpectedly. "
                            f"Restarting... (attempt {retry_count + 1}/{self.max_retries})"
                        )

                        # Increment retry count
                        self.pipeline_retry_counts[pipeline_name] = retry_count + 1

                        # Release the dead runner's IPC listener before the
                        # relaunch replaces it.
                        try:
                            process.close()
                        except Exception:
                            pass

                        # Close old log handle
                        if pipeline_name in self.pipeline_log_handles:
                            try:
                                self.pipeline_log_handles[pipeline_name].close()
                            except:
                                pass

                        if stop_flag.is_set():
                            self.logger.info(
                                f"[VA][monitor] '{pipeline_name}' is being stopped; skipping restart"
                            )
                            break

                        # Restart pipeline using saved parameters
                        params = self.pipeline_params.get(pipeline_name)
                        if params:
                            try:
                                self._launch_pipeline_internal(
                                    pipeline_name, params["options"], params["description"]
                                )
                            except Exception as e:
                                self.logger.error(
                                    f"[VA][monitor] restart of pipeline '{pipeline_name}' raised: {e}",
                                    exc_info=True,
                                )
                                # The pipeline is dead and will not be retried:
                                # a terminal state, not a reason to stop
                                # reporting. Leaving this bare let the session's
                                # "va" stage sit on "running" for good.
                                self._finalize_pipeline(pipeline_name, "failed")
                                break
                        else:
                            self.logger.error(
                                f"Cannot restart pipeline '{pipeline_name}': parameters not found"
                            )
                            self._finalize_pipeline(pipeline_name, "failed")
                            break
                    else:
                        self.logger.error(
                            f"Pipeline '{pipeline_name}' reached maximum retry limit ({self.max_retries}). "
                            f"Giving up."
                        )
                        self._finalize_pipeline(pipeline_name, "failed")
                        break

            # Check every 2 seconds
            time.sleep(2)

        self.logger.info(f"Monitor thread for pipeline '{pipeline_name}' stopped")

    def _finalize_pipeline(self, pipeline_name: str, status: str) -> None:
        """Record a pipeline's terminal status, then fire the all-done callback."""
        self.pipeline_final_status.setdefault(pipeline_name, status)
        self._fire_done_callback_if_all_finished()

    def _fire_done_callback_if_all_finished(self):
        """Fire on_all_pipelines_done once when no pipeline processes remain running."""
        if self._reports_generated:
            self.logger.info("[VA][done] skip: _reports_generated already set")
            return
        if not self._any_pipeline_ran:
            self.logger.info("[VA][done] skip: _any_pipeline_ran is False")
            return
        still_running = [
            name for name, proc in self.pipelines.items()
            if proc.poll() is None
        ]
        if still_running:
            self.logger.info(f"[VA][done] deferring, still running: {still_running}")
            return
        self._reports_generated = True
        self.logger.info("[VA] All pipelines finished — triggering engagement report generation.")
        if callable(self.on_all_pipelines_done):
            try:
                self.on_all_pipelines_done(getattr(self, "x_session_id", None))
                self.logger.info("[VA][done] callback returned")
            except Exception as exc:
                self.logger.error(f"[VA] on_all_pipelines_done callback raised: {exc}", exc_info=True)
        else:
            self.logger.warning("[VA][done] no on_all_pipelines_done callback configured")

    def _launch_pipeline_internal(
        self, pipeline_name: str, options: PipelineOptions, description: str
    ) -> bool:
        """
        Internal method to launch pipeline (used for initial launch and restarts)

        Args:
            pipeline_name: Name of pipeline
            options: Pipeline options
            description: gst-parse pipeline description string

        Returns:
            True if pipeline launched successfully, False otherwise

        Raises:
            RuntimeError: the pipeline failed to reach PLAYING. The message
                carries the element name and error text straight from the bus.
        """
        log_handle = None
        try:
            # The runner writes its own logging here.
            log_dir = Path(options.output_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{pipeline_name}_{int(time.time())}.log"
            log_handle = open(log_file, "w", buffering=1)  # Line buffered

            runner = PipelineRunnerClient(pipeline_name, description, log_handle)

            # Store handle, log file, and log handle before starting so a
            # failure mid-handshake still leaves the state cleanable.
            self.pipelines[pipeline_name] = runner
            self.pipeline_logs[pipeline_name] = log_file
            self.pipeline_log_handles[pipeline_name] = log_handle

            # Blocks until the pipeline reports PLAYING on its bus
            runner.start(ready_timeout=self.pipeline_ready_timeout)

            self.logger.info(
                f"Pipeline '{pipeline_name}' started with PID: {runner.pid}"
            )
            self.logger.info(f"  Log file: {log_file}")

            return True

        except RuntimeError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to launch pipeline '{pipeline_name}': {e}")
            if log_handle:
                try:
                    log_handle.close()
                except Exception:
                    pass
            return False

    def _build_pipeline_front(
        self, source: str, options: PipelineOptions, input_type: str
    ) -> List[str]:
        """Build front camera pipeline (Pipeline 1). See pipeline_builders.py."""
        return build_pipeline_front(self, source, options, input_type)

    def _build_pipeline_back(
        self, source: str, options: PipelineOptions, input_type: str
    ) -> List[str]:
        """Build back camera pipeline (Pipeline 2). See pipeline_builders.py."""
        return build_pipeline_back(self, source, options, input_type)

    def _build_pipeline_content(
        self, source: str, options: PipelineOptions, input_type: str
    ) -> List[str]:
        """Build content/file pipeline (Pipeline 3). See pipeline_builders.py."""
        return build_pipeline_content(self, source, options, input_type)

    def launch_pipeline(
        self, pipeline_name: str, source: str, options: Optional[PipelineOptions] = None
    ) -> bool:
        """
        Launch a pipeline by name

        Args:
            pipeline_name: Name of pipeline ('front', 'back', or 'content')
            source: Input source (RTSP URL or file path)
            options: Optional pipeline configuration options

        Returns:
            True if pipeline launched successfully, False otherwise

        Note:
            - Source can be RTSP URL (rtsp://...) or local file path
            - Input type is auto-detected from source (starts with 'rtsp://' = RTSP, else file)
            - Video output is pushed to RTSP server (configured via options.output_rtsp)
              unless options.output_stream is False, in which case it is discarded
            - Metadata is saved to files in options.output_dir
        """
        # Validate pipeline name
        valid_names = [p.value for p in PipelineName]
        if pipeline_name not in valid_names:
            self.logger.error(
                f"Invalid pipeline name: {pipeline_name}. Valid names: {valid_names}"
            )
            return False

        # Check if pipeline is already running
        if pipeline_name in self.pipelines and self.is_pipeline_running(pipeline_name):
            self.logger.warning(f"Pipeline '{pipeline_name}' is already running")
            return False

        # Use default options if not provided
        if options is None:
            options = PipelineOptions()

        # Resolve the device to use for this launch (VA-10): an explicit
        # options.device always wins; otherwise fall back to this pipeline's
        # application-level default (self.default_device, env-var
        # configurable per pipeline -- see __init__).
        if options.device is None:
            options = replace(options, device=self.default_device[pipeline_name])

        # Setup environment before any GStreamer process runs, so gst-discoverer
        # below and the pipeline itself see the same GST_PLUGIN_PATH and share
        # one plugin registry cache.
        self._setup_environment()

        # Reject sources that could break out of the gst-parse pipeline
        # description (quote/backslash/'!' or control chars). Defense-in-depth:
        # the app layer validates too, but the side-car must not trust callers
        # on the internal network either.
        if any(c in '"\\!' for c in source) or any(ord(c) < 0x20 for c in source):
            raise ValueError("source contains invalid characters")

        # Auto-detect input type from source
        if source.startswith("rtsp://"):
            input_type = "rtsp"
        else:
            input_type = "file"
            # Verify file exists
            if not Path(source).exists():
                self.logger.error(f"Source file not found: {source}")
                raise ValueError(f"Source file not found: {source}")
            # Validate file with gst-discoverer
            error_msg = self._validate_file_with_discoverer(source)
            if error_msg:
                raise ValueError(f"Invalid source file '{source}': {error_msg}")

        try:
            # Build pipeline based on name
            if pipeline_name == PipelineName.FRONT.value:
                pipeline_elements = self._build_pipeline_front(
                    source, options, input_type
                )
            elif pipeline_name == PipelineName.BACK.value:
                pipeline_elements = self._build_pipeline_back(
                    source, options, input_type
                )
            elif pipeline_name == PipelineName.CONTENT.value:
                pipeline_elements = self._build_pipeline_content(
                    source, options, input_type
                )
            else:
                raise ValueError(f"Unknown pipeline: {pipeline_name}")

            # Join into a description for Gst.parse_launch in the runner.
            description = self._join_pipeline_description(pipeline_elements)

            self.logger.info(f"Launching pipeline '{pipeline_name}'")
            self.logger.info(f"  Source: {source} (type: {input_type})")
            self.logger.info(
                f"  RTSP output: {options.output_rtsp}"
                if options.output_stream
                else "  RTSP output: disabled (fakesink)"
            )
            self.logger.info(f"  Metadata dir: {options.output_dir}")
            self.logger.info(f"Pipeline: {description}")

            # Store output files for monitoring
            output_files = []
            if pipeline_name == PipelineName.FRONT.value:
                output_files = [
                    Path(options.output_dir) / "front_resnet18.txt",
                    Path(options.output_dir) / "front_posture.txt",
                    Path(options.output_dir) / "front_mobilenetv2.txt",
                ]
            elif pipeline_name == PipelineName.BACK.value:
                output_files = [
                    Path(options.output_dir) / "back_posture.txt",
                    Path(options.output_dir) / "back_resnet18.txt",
                ]
            elif pipeline_name == PipelineName.CONTENT.value:
                output_files = [Path(options.output_dir) / "content_results.txt"]
            self.pipeline_output_files[pipeline_name] = output_files

            # Save pipeline parameters for restart capability
            self.pipeline_params[pipeline_name] = {
                "options": options,
                "description": description,
            }

            # Initialize retry count
            self.pipeline_retry_counts[pipeline_name] = 0
            # Clear any prior final status for a fresh launch
            self.pipeline_final_status.pop(pipeline_name, None)

            # Launch pipeline
            success = self._launch_pipeline_internal(pipeline_name, options, description)

            if not success:
                return False

            # ---- START RTSP RECORDING ----
            if options.record:
                recorder_name = f"{pipeline_name}_recorder"

                # Derive from options.output_dir (the single source of truth
                # for everything this launch call writes), not a separate
                # RuntimeConfig/x_session_id lookup -- the HTTP control API
                # has no way to set self.x_session_id, so that path was
                # unreachable from any real caller.
                output_video_path = os.path.join(
                    options.output_dir,
                    f"{pipeline_name}.mp4"
                )

                start_rtsp_recording(
                    name=recorder_name,
                    rtsp_url=source,
                    output_file=output_video_path,
                )

            # Start monitoring thread
            stop_flag = threading.Event()
            self.monitor_stop_flags[pipeline_name] = stop_flag

            monitor_thread = threading.Thread(
                target=self._monitor_pipeline,
                args=(pipeline_name,),
                daemon=True,
                name=f"monitor-{pipeline_name}",
            )
            monitor_thread.start()
            self.monitor_threads[pipeline_name] = monitor_thread

            self.logger.info(
                f"Started monitoring thread for pipeline '{pipeline_name}'"
            )
            self._any_pipeline_ran = True

            return True

        except RuntimeError:
            raise
        except Exception as e:
            self.logger.error(f"Failed to launch pipeline '{pipeline_name}': {e}")
            return False

    def stop_pipeline(self, pipeline_name: str, timeout: float = 10.0) -> bool:
        """
        Stop a running pipeline

        Args:
            pipeline_name: Name of pipeline to stop
            timeout: Maximum time to wait for graceful shutdown (seconds)

        Returns:
            True if pipeline stopped successfully, False otherwise
        """
        pipeline_name = pipeline_name.lower()

        if pipeline_name not in self.pipelines:
            self.logger.warning(f"Pipeline '{pipeline_name}' is not registered")
            return False

        # Retire the monitor before anything touches the process.
        if pipeline_name in self.monitor_stop_flags:
            self.monitor_stop_flags[pipeline_name].set()

        stop_rtsp_recording(f"{pipeline_name}_recorder")
        process = self.pipelines[pipeline_name]

        if process.poll() is not None:
            self.logger.info(f"Pipeline '{pipeline_name}' is not running")
            del self.pipelines[pipeline_name]
            # It ended on its own before the stop arrived. Ask the runner how it
            # went rather than assuming the worst - the monitor polls only every
            # two seconds, so a clean EOS often lands here first. setdefault
            # inside _finalize_pipeline keeps the monitor's verdict if it won.
            self._finalize_pipeline(
                pipeline_name, "eos" if process.exited_normally() else "failed"
            )
            return True

        try:
            self.logger.info(
                f"Stopping pipeline '{pipeline_name}' (PID: {process.pid})"
            )

            # Graceful shutdown: ask the runner to push EOS through the
            # pipeline so sinks and muxers finalise their output.
            if not process.request_stop():
                self.logger.warning(
                    f"Pipeline '{pipeline_name}' unreachable over IPC; terminating"
                )
                process.terminate()

            try:
                process.wait(timeout=timeout)
                self.logger.info(f"Pipeline '{pipeline_name}' stopped gracefully")
            except subprocess.TimeoutExpired:
                self.logger.warning(
                    f"Pipeline did not stop within {timeout}s, forcing kill..."
                )
                process.kill()
                process.wait(timeout=5)
                self.logger.info(f"Pipeline '{pipeline_name}' killed")
            finally:
                process.close()

            del self.pipelines[pipeline_name]
            self._finalize_pipeline(pipeline_name, "stopped")

            if pipeline_name in self.monitor_threads:
                monitor_thread = self.monitor_threads[pipeline_name]
                monitor_thread.join(timeout=2.0)
                del self.monitor_threads[pipeline_name]

            # Clean up associated data
            if pipeline_name in self.pipeline_logs:
                del self.pipeline_logs[pipeline_name]
            if pipeline_name in self.pipeline_log_handles:
                # Close the log file handle
                try:
                    self.pipeline_log_handles[pipeline_name].close()
                except:
                    pass
                del self.pipeline_log_handles[pipeline_name]
            if pipeline_name in self.pipeline_output_files:
                del self.pipeline_output_files[pipeline_name]
            if pipeline_name in self.pipeline_params:
                del self.pipeline_params[pipeline_name]
            if pipeline_name in self.pipeline_retry_counts:
                del self.pipeline_retry_counts[pipeline_name]
            if pipeline_name in self.monitor_stop_flags:
                del self.monitor_stop_flags[pipeline_name]
            if pipeline_name in self.pipeline_errors:
                del self.pipeline_errors[pipeline_name]

            return True

        except Exception as e:
            self.logger.error(f"Error stopping pipeline '{pipeline_name}': {e}")
            # Only call it over if the process really is gone. A stop that threw
            # on the way in may well have left it running, and recording a
            # terminal status here would - via setdefault - override whatever
            # the monitor thread later decides. When it is still alive the
            # monitor is still watching it, so say nothing.
            if process.poll() is not None:
                self._finalize_pipeline(pipeline_name, "failed")
            return False

    def is_pipeline_running(self, pipeline_name: str) -> bool:
        """Check if a pipeline is currently running"""
        pipeline_name = pipeline_name.lower()

        if pipeline_name not in self.pipelines:
            return False

        process = self.pipelines[pipeline_name]
        return process.poll() is None

    async def monitor_pipeline_status(
        self, check_interval: float = 2.0
    ):
        """
        Monitor all pipeline processes status and yield status updates for streaming response

        This async generator continuously monitors all pipeline process states and yields
        combined status information. It does NOT restart the pipelines - that is handled by
        the internal _monitor_pipeline thread.

        Args:
            check_interval: Seconds between status checks (default: 2.0)

        Yields:
            Dictionary with status information for all pipelines:
            - pipelines: List of pipeline status dictionaries, each containing:
                - pipeline_name: Name of the pipeline
                - status: 'running', 'stopped_normal', 'stopped_error', or 'not_found'
                - pid: Process ID (if running)
                - message: Additional status message
                - error: Error details (if stopped with error)

        Note:
            This is designed for streaming responses. The _monitor_pipeline thread
            handles automatic restarts, so this function only reports status.
        """
        import asyncio

        all_pipeline_names = ["front", "back", "content"]
        self.logger.info(f"Starting status monitoring for all pipelines: {all_pipeline_names}")

        try:
            while True:
                pipeline_statuses = []
                any_live = False
                any_awaiting_restart = False

                for pipeline_name in all_pipeline_names:
                    pipeline_name_lower = pipeline_name.lower()
                    # Set by _finalize_pipeline, and the only authority on "this
                    # one is never coming back": "eos", "failed" or "stopped".
                    final_status = self.pipeline_final_status.get(pipeline_name_lower)

                    # Check if pipeline is registered
                    if pipeline_name_lower not in self.pipelines:
                        pipeline_statuses.append({
                            "pipeline_name": pipeline_name,
                            "status": "not_found",
                            "final_status": final_status,
                            "message": f"Pipeline '{pipeline_name}' not found",
                        })
                        continue

                    process = self.pipelines[pipeline_name_lower]
                    return_code = process.poll()

                    # Collect any error events recorded by the monitor thread
                    errors = self.pipeline_errors.pop(pipeline_name_lower, [])

                    # Pipeline is running
                    if return_code is None:
                        any_live = True
                        status_entry = {
                            "pipeline_name": pipeline_name,
                            "status": "running",
                            "final_status": final_status,
                            "pid": process.pid,
                        }
                        if errors:
                            status_entry["errors"] = errors
                        pipeline_statuses.append(status_entry)

                    # Pipeline has stopped
                    else:
                        # Dead but not finalised means the monitor thread is
                        # between a crash and its relaunch. Reported as such so
                        # the client shows "retrying" rather than "failed", and
                        # counted so this stream stays open across the gap.
                        if final_status is None:
                            any_awaiting_restart = True

                        # Normal-vs-error comes from the runner's bus events.
                        if process.exited_normally():
                            pipeline_statuses.append({
                                "pipeline_name": pipeline_name,
                                "status": "stopped_normal",
                                "final_status": final_status,
                                "return_code": return_code,
                                "message": "Pipeline exited normally (EOS received)",
                            })
                        else:
                            if not errors:
                                reported = process.error_text()
                                if reported:
                                    errors = [reported]

                            pipeline_statuses.append({
                                "pipeline_name": pipeline_name,
                                "status": "stopped_error",
                                "final_status": final_status,
                                "return_code": return_code,
                                "message": "Pipeline exited unexpectedly.",
                                "errors": errors,
                            })

                # Yield combined status
                yield {"pipelines": pipeline_statuses}

                # Close the stream once there is nothing left to report.
                if self._any_pipeline_ran and not any_live and not any_awaiting_restart:
                    self.logger.info(
                        "[VA][status] all pipelines settled; ending status stream"
                    )
                    return

                await asyncio.sleep(check_interval)

        except Exception as e:
            self.logger.error(f"Error monitoring pipeline status: {e}")
            yield {
                "pipeline_name": pipeline_name,
                "status": "error",
                "error": str(e),
                "message": "Monitoring error occurred",
            }

    def monitor_pipeline_result(
        self, pipeline_name: str, file_name: Optional[str] = None
    ) -> Generator[Dict, None, None]:
        """
        Monitor pipeline output file and yield JSON objects as new lines are written

        Args:
            pipeline_name: Name of pipeline to monitor
            file_name: Specific output file to monitor (e.g., "front_resnet18.txt")
                      If None, monitors the first output file for the pipeline

        Yields:
            Dictionary parsed from each new JSON line in the output file

        Note:
            This is a blocking generator that continuously monitors the file.
            Use Ctrl+C or call stop_pipeline() to stop monitoring.
        """
        pipeline_name = pipeline_name.lower()

        if pipeline_name not in self.pipelines:
            self.logger.error(f"Pipeline '{pipeline_name}' is not registered")
            return

        if pipeline_name not in self.pipeline_output_files:
            self.logger.error(
                f"No output files registered for pipeline '{pipeline_name}'"
            )
            return

        # Determine which file to monitor
        output_files = self.pipeline_output_files[pipeline_name]
        if not output_files:
            self.logger.error(f"No output files found for pipeline '{pipeline_name}'")
            return

        if file_name:
            # Find the matching file
            target_file = None
            for f in output_files:
                if f.name == file_name or str(f) == file_name:
                    target_file = f
                    break
            if not target_file:
                self.logger.error(
                    f"File '{file_name}' not found in pipeline output files: {[str(f) for f in output_files]}"
                )
                return
        else:
            # Use the first file
            target_file = output_files[0]

        self.logger.info(f"Monitoring file: {target_file}")

        # Wait for file to be created
        timeout = 30
        start_time = time.time()
        while not target_file.exists():
            if time.time() - start_time > timeout:
                self.logger.error(f"Timeout waiting for file: {target_file}")
                return
            if not self.is_pipeline_running(pipeline_name):
                self.logger.error(
                    f"Pipeline stopped before file was created: {target_file}"
                )
                return
            time.sleep(0.5)

        # Monitor the file for new lines
        try:
            with open(target_file, "r") as f:
                # Seek to the end of existing content
                f.seek(0, 2)

                while self.is_pipeline_running(pipeline_name):
                    line = f.readline()
                    if line:
                        line = line.strip()
                        if line:
                            try:
                                # Parse JSON and yield
                                json_obj = json.loads(line)
                                yield json_obj
                            except json.JSONDecodeError as e:
                                self.logger.warning(f"Failed to parse JSON line: {e}")
                                self.logger.debug(f"Line content: {line}")
                    else:
                        # No new data, wait a bit
                        time.sleep(0.1)

                self.logger.info(
                    f"Pipeline '{pipeline_name}' stopped, ending monitoring"
                )

        except Exception as e:
            self.logger.error(f"Error monitoring file: {e}")
            return

    def get_pipeline_status(self, pipeline_name: str) -> Optional[Dict]:
        """
        Get status information for a pipeline (non-blocking)

        Args:
            pipeline_name: Name of pipeline to check

        Returns:
            Dictionary with pipeline status information, or None if not found
        """
        pipeline_name = pipeline_name.lower()

        if pipeline_name not in self.pipelines:
            return None

        process = self.pipelines[pipeline_name]

        status = {
            "name": pipeline_name,
            "running": process.poll() is None,
            "pid": process.pid,
            "return_code": process.poll(),
            # Terminal verdict once the pipeline has stopped on its own (EOS),
            # failed after exhausting retries, or been stopped manually: one of
            # "eos" | "failed" | "stopped", or None while still running.
            "final_status": self.pipeline_final_status.get(pipeline_name),
        }

        # Add process details if running
        if status["running"]:
            try:
                proc = psutil.Process(process.pid)
                status["cpu_percent"] = proc.cpu_percent()
                status["memory_mb"] = proc.memory_info().rss / 1024 / 1024
                status["uptime_seconds"] = time.time() - proc.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Add log file info
        if pipeline_name in self.pipeline_logs:
            status["log_file"] = str(self.pipeline_logs[pipeline_name])

        # Add output files info
        if pipeline_name in self.pipeline_output_files:
            status["output_files"] = [
                str(f) for f in self.pipeline_output_files[pipeline_name]
            ]

        return status

    def get_all_pipelines_status(self) -> Dict[str, Dict]:
        """Get status of all registered pipelines (non-blocking)"""
        return {name: self.get_pipeline_status(name) for name in self.pipelines.keys()}

    def stop_all_pipelines(self, timeout: float = 10.0) -> bool:
        """Stop all running pipelines"""
        self.logger.info("Stopping all pipelines...")
        success = True

        for pipeline_name in list(self.pipelines.keys()):
            if not self.stop_pipeline(pipeline_name, timeout):
                success = False

        return success

    def _cleanup(self):
        """Cleanup handler called on process exit"""
        if self.pipelines:
            self.logger.info("Cleaning up pipelines on exit...")

            # Stop all monitoring threads first
            for stop_flag in self.monitor_stop_flags.values():
                stop_flag.set()

            # Wait for monitoring threads to finish
            for thread in self.monitor_threads.values():
                thread.join(timeout=2.0)

            self.stop_all_pipelines(timeout=5.0)

            # Close any remaining log file handles
            for handle in self.pipeline_log_handles.values():
                try:
                    handle.close()
                except:
                    pass
            self.pipeline_log_handles.clear()

    def get_pose_stats(
        self, front_posture_file: str, previous_state: Optional[Dict] = None
    ) -> tuple[Dict, Dict]:
        """
        Incrementally analyze front_posture.txt and generate pose statistics.
        Only processes new lines since last call, reusing previous results.

        Args:
            front_posture_file: Path to front_posture.txt file
            previous_state: State from previous call (contains processed_lines,
            frames, counters, etc.)

        Returns:
            Tuple of (statistics_dict, new_state_dict)
        """
        return self._pose_stats_tracker.get_pose_stats(front_posture_file, previous_state)

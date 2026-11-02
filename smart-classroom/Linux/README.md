# Smart Classroom

The **Smart Classroom** project is a modular, extensible framework designed to process and summarize educational content using advanced AI models for the Linux OS. It supports transcription, summarization, mindmap generation, future capabilities like video understanding, multimodal content search, and real-time video analytics.

## Main Features

The main features are as follows:

### Audio Pipeline

- Audio transcription with ASR models (e.g., Whisper, Paraformer)
- Speaker diarization using Pyannote Audio models (optional)
- Summarization using powerful LLMs (e.g., Qwen, LLaMA)
- MindMap generation using Mermaid.js for visual diagram rendering of the summary
- Content segmentation for automatic topic extraction from transcripts
- Timestamped topic indexing and semantic search across classroom sessions

### Video Analytics Pipeline

- Real-time video analysis across three concurrent camera streams (front, back, content)
- Person detection and pose estimation using YOLO models with keypoint skeleton tracking
- Person re-identification for tracking individual students across frames
- Classroom statistics including student count, stand-up events, and hand-raise events

### Content Search

- Multimodal semantic search across videos, images, and documents
- AI-driven video summarization using Vision Language Models (VLM)
- Document text extraction with OCR support (PaddleOCR)
- RAG-based Question & Answer over uploaded educational materials
- Vector-based retrieval using ChromaDB with CLIP and mE5 embeddings

Topic segmentation automatically enables Content Search.

- The reranker requires an Intel GPU.
- Run `./setup.sh` to configure GPU access and start the app.

For manual startup, see the [Advanced Setup Guide](../docs/user-guide/advanced-setup-guide.md#linux-manual-docker-startup).

## Architecture

- Plug-and-play architecture for integrating new ASR, LLM, and video analytics models
- API-first design ready for frontend integration.

### OVMS pipeline configuration

`models.text_gen.ovms.pipeline_type` defaults to `VLM`. Setup copies it into
`config.pipeline_type` in both startup-models configurations; the model downloader
passes it to `export_model.py text_generation --pipeline_type`. Generating a
startup configuration with `python -m utils.ovms_provision --write-startup-config`
preserves the same setting. Valid values are `LM`, `LM_CB`, `VLM`, `VLM_CB` and
`AUTO`.

Run `./setup.sh` after changing this setting. It controls model export/serving
configuration, not individual chat requests; editing it does not change an
already-running OVMS pipeline.

## Get Started

To see the system requirements and other installations, see the following guides:

- [System Requirements](../docs/user-guide/get-started/system-requirements.md): Check the hardware and software requirements for deploying the application.
- [Get Started](../docs/user-guide/get-started/get-started-linux.md): Follow step-by-step instructions to set up the application.

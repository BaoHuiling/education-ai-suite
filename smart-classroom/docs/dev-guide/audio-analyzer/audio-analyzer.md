<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Audio Analyzer

The Audio Analyzer microservice (`edge-ai-libraries/microservices/audio-analyzer`)
provides transcription and optional speaker diarization for the audio pipeline.
The provider is set by `models.asr.provider` in `config.yaml`. On Linux the
Audio Analyzer container requests its model snapshots from the shared
`model-downloader` container on first start (they are cached on the
`model-cache` volume afterwards); it does not download them itself.

| Provider | Models | Downloaded from |
|---|---|---|
| OpenVINO™ (default) | Whisper (e.g. `whisper-base`) | Hugging Face (`openai/whisper-*`) via `model-downloader`, exported to OpenVINO IR |
| FunASR | Paraformer (`paraformer-zh`), for Mandarin | ModelScope via `model-downloader` (`modelscope` plugin) |
| OpenAI | Whisper (e.g. `whisper-base`) | OpenAI's public `openai-whisper` checkpoints (fetched directly by Audio Analyzer) |

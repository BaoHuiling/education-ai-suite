<!-- SPDX-FileCopyrightText: (C) 2026 Intel Corporation -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Audio Analyzer

The Audio Analyzer microservice (`edge-ai-libraries/microservices/audio-analyzer`)
provides transcription and optional speaker diarization for the audio pipeline.
The provider is set by `models.asr.provider` in `config.yaml`, and models are
downloaded on first start, then cached.

| Provider | Models | Downloaded from |
|---|---|---|
| OpenVINO™ (default) | Whisper (e.g. `whisper-base`) | Hugging Face (`openai/whisper-*`), exported to OpenVINO IR |
| FunASR | Paraformer (`paraformer-zh`), for Mandarin | ModelScope (switch to Hugging Face with `model_hub`) |
| OpenAI | Whisper (e.g. `whisper-base`) | OpenAI's public `openai-whisper` checkpoints |

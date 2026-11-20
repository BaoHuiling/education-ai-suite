# Configuration References

This folder stores reference configuration for the upstream services used by
AI Teaching Assistant:

- [configs/audio-analyzer/config.yaml](./audio-analyzer/config.yaml)
- [configs/text-to-speech/config.yaml](./text-to-speech/config.yaml)

## Windows Dependency Pins

[windows-dependency-pins.psd1](./windows-dependency-pins.psd1) is the source of truth
for the Windows compatibility pins used by [setup_windows.ps1](../setup_windows.ps1).
Each entry records the exact pip `Requirement` and affected `Services`.
A service scope of `"*"` applies to all four service
environments; other scopes must match setup's service names.

Edit this file to add or change a compatibility pin, then rerun setup. Setup reads
it directly and resolves the matching pins together with each service's
requirements. Keep the `ml-dtypes` pin audio-only because RAG uses NumPy 1.26.
Validate replacements with Application Control enabled, run `pip check`, and
verify model warmup in the affected environments before updating a pin.

This file tracks compatibility overrides, not a complete dependency lockfile.
The service requirements files still define their normal dependencies, including
the Windows RAG subset in
[rag-service.requirements.windows.txt](./rag-service.requirements.windows.txt).
See [troubleshooting](../docs/user-guide/troubleshooting.md#smart-app-control-blocks-cygrpc--model-warmup-fails)
for the observed native-extension failures and verification commands.

## Important Runtime Note

In this Windows-native application flow, services run directly from the
submodule source trees. The active runtime configs are read from:

- [edge-ai-libraries/microservices/audio-analyzer/config.yaml](https://github.com/open-edge-platform/edge-ai-libraries/blob/release-2026.3.0/microservices/audio-analyzer/config.yaml)
- [edge-ai-libraries/microservices/text-to-speech/config.yaml](https://github.com/open-edge-platform/edge-ai-libraries/blob/release-2026.3.0/microservices/text-to-speech/config.yaml)
- [voice-enabled-interactions/smart-kiosk-assistant/rag-service/config.yaml](https://github.com/intel-retail/voice-enabled-interactions/blob/release-2026.3.0/smart-kiosk-assistant/rag-service/config.yaml)

If you change values in the reference YAML files above, copy those values to the
runtime config files before restarting services. The Windows dependency pins and
RAG requirements file are read directly by setup and do not need to be copied.

## Why Keep This Folder

- Maintains a project-owned baseline for ASR and TTS behavior.
- Helps track intentional config choices separately from upstream defaults.
- Makes it easier to compare and review config changes.

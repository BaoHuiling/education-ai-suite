# AI Teaching Assistant

AI Teaching Assistant is a Windows-native, voice-first educational assistant.
It captures speech in the browser, transcribes with Whisper, retrieves grounded
context from uploaded documents, generates answers with a local LLM, and plays
voice responses back in the UI.

The application runs locally on Intel hardware using OpenVINO-backed services.

## What This Project Contains

- A React browser UI in [assistant-react-ui](assistant-react-ui).
- A FastAPI UI server/proxy in [ata_ui_server.py](ata_ui_server.py).
- A FastAPI orchestrator (`kiosk-core`) in [voice-enabled-interactions/smart-kiosk-assistant/main.py](https://github.com/intel-retail/voice-enabled-interactions/blob/main/smart-kiosk-assistant/main.py).
- A RAG microservice in [voice-enabled-interactions/smart-kiosk-assistant/rag-service](https://github.com/intel-retail/voice-enabled-interactions/tree/main/smart-kiosk-assistant/rag-service).
- Upstream ASR and TTS services from the `edge-ai-libraries` submodule.
- Windows setup/start/stop scripts in [setup_windows.ps1](setup_windows.ps1), [start_ata.ps1](start_ata.ps1), and [stop_ata.ps1](stop_ata.ps1).

## Runtime Topology

| Service | Port | Role | Entry Point |
|---|---|---|---|
| `ai-teaching-assistant ui` | `7860` | Serves React app and proxies API calls | [ata_ui_server.py](ata_ui_server.py) |
| `kiosk-core` | `8012` | Session orchestration (audio flow, polling state, response audio) | [voice-enabled-interactions/smart-kiosk-assistant/main.py](https://github.com/intel-retail/voice-enabled-interactions/blob/main/smart-kiosk-assistant/main.py) |
| `audio-analyzer` | `8010` | Speech-to-text (Whisper) | [edge-ai-libraries/microservices/audio-analyzer/main.py](https://github.com/open-edge-platform/edge-ai-libraries/blob/main/microservices/audio-analyzer/main.py) |
| `text-to-speech` | `8011` | Speech synthesis | [edge-ai-libraries/microservices/text-to-speech/main.py](https://github.com/open-edge-platform/edge-ai-libraries/blob/main/microservices/text-to-speech/main.py) |
| `rag-service` | `8020` | Document ingestion, retrieval, and answer generation | [voice-enabled-interactions/smart-kiosk-assistant/rag-service/main.py](https://github.com/intel-retail/voice-enabled-interactions/blob/main/smart-kiosk-assistant/rag-service/main.py) |
| `metrics-collector` | `9000` | Platform and runtime metrics | [metrics_collector/windows/metrics_collector.ps1](metrics_collector/windows/metrics_collector.ps1) |

## Quick Start

See [docs/user-guide/get-started.md](docs/user-guide/get-started.md) for the complete flow.

> [!IMPORTANT]
> Use **Windows PowerShell** (not Command Prompt/CMD) for all steps in this guide.
> PowerShell scripts (`.ps1` files) will not execute in CMD — they will only open as text files.

At a high level:

```powershell
git clone --filter=blob:none --sparse -b main https://github.com/open-edge-platform/education-ai-suite.git;
cd education-ai-suite;
git sparse-checkout set ai-teaching-assistant;
cd ai-teaching-assistant;
.\setup_windows.ps1;
.\start_ata.ps1
```

Open `http://127.0.0.1:7860`.

## Wake-Word Voice Activation

A wake word starts a voice turn hands-free. Two backends are available, selected
with `ATA_WAKEWORD_BACKEND` in [.env](.env):

| Backend | Value | Wake word | Notes |
|---|---|---|---|
| Software (default) | `software` | "hey jarvis" | openWakeWord on the CPU. Works on any machine. Turn on **Wake-word mode** in the UI. |
| Hardware | `hardware` | "Cortana" | Intel always-on DSP through ATA's packaged Windows voice agent. Requires an Intel device whose Smart Sound driver provides the keyword model. |

`setup_windows.ps1` prompts for this during setup. After changing `.env`,
restart ATA (`stop_ata.ps1`, then `start_ata.ps1`).

### Software wake word ("hey jarvis")

Open the UI, turn on **Wake-word mode**, and say "hey jarvis", then your question.
The browser records the audio and `kiosk-core` runs openWakeWord on it.

### Hardware wake word ("Cortana")

In both backends the question is recorded by the **browser**, exactly like the
mic button. In hardware mode, the DSP detects "Cortana", Windows starts ATA's
wake agent, and the agent tells the UI server, which makes the open browser UI
start the turn.

The software wake-word toggle is disabled in hardware mode. The Cortana status
shows hardware wake activity; the software detection score is hidden.

Hardware wake requires Microsoft LAF approval and private local configuration.
Follow the [complete hardware setup](wakeword-agent/README.md#setup-first-time),
including the [token requirements](wakeword-agent/README.md#microsoft-approval-and-private-access-configuration).
Do not place a token in source or ATA's `.env`.

From the `ai-teaching-assistant` directory, install once in elevated PowerShell:

```powershell
.\wakeword-agent\package.ps1 -Install
```

Reboot, then provision the private file described in the hardware guide, using
normal Windows PowerShell as the signed-in user:

```powershell
.\wakeword-agent\configure-access.ps1
```

After every sign-in, start ATA with `start_ata.ps1`, wait for the services to be
ready, and run this in a second terminal at `ai-teaching-assistant`:

```powershell
.\start_wakeword.ps1
```

Open the UI, allow microphone access, and click on the page. Say "Cortana", wait
for recording, then ask the question. Installation alone does not arm the keyword
after sign-in.

See [wakeword-agent/README.md](wakeword-agent/README.md) for how it works,
verification, updating, and how to restore the previous keyword binding.

### Switching backends

| To | Steps |
|---|---|
| Software | Set `ATA_WAKEWORD_BACKEND=software`, restart ATA, turn on **Wake-word mode** in the UI. To stop "Cortana" waking the agent, don't run `start_wakeword.ps1` after signing in, or give the keyword back with `register-keyword.ps1 -Restore`. |
| Hardware | Set `ATA_WAKEWORD_BACKEND=hardware`, restart ATA, run `start_wakeword.ps1`, reload the UI and click on the page. |

`mic_session.py --wakeword` also runs "hey jarvis" detection from a terminal
on the host microphone. On some microphone arrays host capture is much less
accurate than browser capture, so prefer the UI toggle.


## Documentation Map

- Overview: [docs/user-guide/index.md](docs/user-guide/index.md)
- Setup: [docs/user-guide/get-started.md](docs/user-guide/get-started.md)
- Application flow (UI walkthrough): [docs/user-guide/application-flow.md](docs/user-guide/application-flow.md)
- Architecture: [docs/user-guide/how-it-works.md](docs/user-guide/how-it-works.md)
- API: [docs/user-guide/api-reference.md](docs/user-guide/api-reference.md)
- Configuration: [docs/user-guide/get-started/configuration.md](docs/user-guide/get-started/configuration.md)
- Standalone/manual service run: [docs/user-guide/get-started/run-standalone.md](docs/user-guide/get-started/run-standalone.md)
- Troubleshooting: [docs/user-guide/troubleshooting.md](docs/user-guide/troubleshooting.md)
- Release notes: [docs/user-guide/release-notes.md](docs/user-guide/release-notes.md)

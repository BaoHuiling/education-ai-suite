# Get Started

Smart Classroom runs on Windows 11 and Ubuntu 24.04 LTS. The services, models, and
features are the same on both systems. The setup procedure differs, so each operating
system has its own guide.

## Overview

On either system, setting up Smart Classroom takes the same four stages:

1. Check that your machine meets the [System Requirements](./get-started/system-requirements.md).
2. Get the application: on Windows, download the installer; on Linux, clone the repository.
3. Configure features, models, and (optionally) speaker diarization in `config.yaml`.
4. Install the prerequisites and start the services: on Windows, from the desktop app;
   on Linux, with `setup.sh`, then open the UI at <http://localhost:5173>.

## Choose Your Operating System

<!--hide_directive ::::{tab-set} hide_directive-->
<!--hide_directive :sync-group: os hide_directive-->

<!--hide_directive :::{tab-item} hide_directive--> Windows
<!--hide_directive :sync: windows hide_directive-->

Desktop app installed from `SmartClassroom-<version>-setup.exe`, available on the
[Releases page](https://github.com/open-edge-platform/education-ai-suite/releases).
The app installs the prerequisites, edits the configuration, and runs the services.

- **OS**: Windows 11
- **Run with**: the **Smart Classroom** desktop app

Continue with [Get Started on Windows](./get-started/get-started-windows.md).

<!--hide_directive ::: hide_directive-->

<!--hide_directive :::{tab-item} hide_directive--> Linux
<!--hide_directive :sync: linux hide_directive-->

Docker-based installation driven by the `setup.sh` script, with the UI served by a
Vite dev server.

- **OS**: Ubuntu 24.04 LTS
- **Run with**: `./setup.sh`

Continue with [Get Started on Linux](./get-started/get-started-linux.md).

<!--hide_directive ::: hide_directive-->

<!--hide_directive :::: hide_directive-->

## Common Configuration

### Speaker Diarization (Optional)

Speaker diarization (identifying who is speaking) is optional and requires a one-time
Hugging Face access token. Request access to the
[Pyannote Speaker Diarization Community v1](https://huggingface.co/pyannote/speaker-diarization-community-1)
model and create a Read token at
[Hugging Face Access Tokens](https://huggingface.co/settings/tokens).
For the steps to apply the token, see the guide for your operating system, or the
[Speaker Diarization Setup](./advanced-setup-guide.md#f-speaker-diarization-setup-optional).

### Network Requirements

- **Proxy**: If you are behind a proxy, configure `HTTP_PROXY` and `HTTPS_PROXY`. On
  Windows, set them on the desktop app's **Configuration** screen. On Linux, configure
  them, and `NO_PROXY`, for your shell and for Docker.
- **Model downloads**: Stable access to `huggingface.co` is required to download
  pre-trained models.

## Learn More

- [System Requirements](./get-started/system-requirements.md): Hardware, software, supported models, and weight formats.
- [Advanced Setup Guide](./advanced-setup-guide.md): Windows configuration reference, troubleshooting, and uninstall.
- [Build the Installer from Source](./get-started/build-from-source-windows.md): Build the Windows installer yourself.
- [Application Flow](./application-flow.md): End-to-end application flow.
- [Content Search Flow](./content-search-flow.md): The flow of the content search functionality.

<!--hide_directive
:::{toctree}
:hidden:

Start on Windows <./get-started/get-started-windows>
Build the Installer from Source <./get-started/build-from-source-windows>
Start on Linux <./get-started/get-started-linux>
./get-started/system-requirements

:::
hide_directive-->

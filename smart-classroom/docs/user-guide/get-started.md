# Get Started

Smart Classroom runs on Windows 11 and Ubuntu 24.04 LTS. The services, models, and
features are the same on both systems. The setup procedure differs, so each operating
system has its own guide.

## Overview

On either system, setting up Smart Classroom takes the same four stages:

1. Check that your machine meets the [System Requirements](./get-started/system-requirements.md).
2. Clone the repository.
3. Configure features, models, and (optionally) speaker diarization in `config.yaml`.
4. Run the setup and start the services, then open the UI: the desktop app on Windows,
   <http://localhost:5173> on Linux.

## Choose Your Operating System

<!--hide_directive ::::{tab-set} hide_directive-->
<!--hide_directive :sync-group: os hide_directive-->

<!--hide_directive :::{tab-item} hide_directive--> Windows
<!--hide_directive :sync: windows hide_directive-->

Native installation with PowerShell, run as a desktop app. The PowerShell scripts with
the browser UI are deprecated and will be removed after release 2027.0.0.

- **OS**: Windows 11
- **Run with**: `.\start-desktop-app.ps1` (or `.\start-smart-classroom.ps1`)

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

- **Proxy**: If you are behind a proxy, configure `HTTP_PROXY`, `HTTPS_PROXY`, and
  `NO_PROXY` for your shell. On Linux, also configure them for Docker.
- **Model downloads**: Stable access to `huggingface.co` is required to download
  pre-trained models.

## Learn More

- [System Requirements](./get-started/system-requirements.md): Hardware, software, supported models, and weight formats.
- [Advanced Setup Guide](./advanced-setup-guide.md): Manual step-by-step setup.
- [Application Flow](./application-flow.md): End-to-end application flow.
- [Content Search Flow](./content-search-flow.md): The flow of the content search functionality.

<!--hide_directive
:::{toctree}
:hidden:

Start on Windows <./get-started/get-started-windows>
Start on Linux <./get-started/get-started-linux>
./get-started/system-requirements

:::
hide_directive-->

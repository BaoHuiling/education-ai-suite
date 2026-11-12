# Get Started on Windows

> [!IMPORTANT]
> Use **Windows PowerShell** (not Command Prompt/CMD) for all steps in this guide.
> PowerShell scripts (`.ps1` files) will not execute in CMD — they will only open as text files.

Smart Classroom runs as a desktop app. The older PowerShell scripts with the browser UI
are deprecated and will be removed after release 2027.0.0. Both use the same services,
models, and configurations.

| | [Desktop app](#desktop-app) (default) | [PowerShell scripts](#deprecated-powershell-scripts-and-the-browser-ui) (deprecated) |
| :--- | :--- | :--- |
| Commands to run | `.\start-desktop-app.ps1` (or `.\start-smart-classroom.ps1`) | `.\setup-smart-classroom.ps1`, then `.\start-smart-classroom.ps1 -WebUI` |
| Prerequisites and installs | **Setup** screen, one item at a time | One scripted pass through everything |
| Settings | **Configuration** screen | Console prompts, then editing `config.yaml` by hand |
| Services | **Services** screen: start, stop, restart, live logs | Separate console windows |
| Where the UI runs | Its own desktop window | Browser at <http://localhost:5173> |

Whichever you pick, start by cloning the repository.

## Clone the Repository

Go to the target directory of your choice and clone the suite.
If you want to clone a specific release branch, replace `main` with the desired tag.
To learn more about partial cloning, see the [Repository Cloning guide](https://docs.openedgeplatform.intel.com/dev/OEP-articles/contribution-guide.html#repository-cloning-partial-cloning).

```bash
git clone --filter=blob:none --sparse --branch main https://github.com/open-edge-platform/education-ai-suite.git
cd education-ai-suite
git sparse-checkout set smart-classroom/Windows
cd smart-classroom/Windows
```

> [!TIP]
> Also want the Flutter + RAG companion app? It's a separate, lighter-weight
> demo under `utils/flutter` at the repository root rather than under
> `smart-classroom/Windows`, so the sparse checkout above does not include
> it. See [Optional: Flutter + RAG Companion App](#optional-flutter--rag-companion-app) below.

---

## Desktop App

The desktop app covers setup, configuration, and running the services in one window.

### Step 1: Launch the App

```powershell
.\start-desktop-app.ps1
```

The script asks for your proxy settings, installs Node.js through winget if it is
missing, builds the UI, and hands over. The app then supervises the Python services
itself. `.\start-smart-classroom.ps1` without `-WebUI` launches the app the same way.

**Optional parameters:**

- `-Dev` — Run the Vite dev server with hot reload
- `-NoAutoStart` — Open the app without starting the backend automatically
- `-SkipNodeInstall` — Fail instead of installing Node.js when it is missing
- `-Silent` — Skip the proxy prompts and use the saved `.proxy-config`

### Step 2: Work Through the Setup Screen

**Get started** opens first and lists what is still missing. Follow its links, or open
**Setup** directly, and work down the list:

| Section | Covers |
| :--- | :--- |
| **System and drivers** | Operating system, processor, memory, free disk space, Intel GPU and NPU drivers, Windows long paths |
| **Software and environment** | Python 3.12, Node.js, FFmpeg, DL Streamer, the Python environment, and the grading conversion environment and layout detection model |

Each row shows what was found and what it blocks. Rows the app can fix have a button:
**Install** for Python, FFmpeg, and Deep Learning Streamer (DL Streamer), **Create** for the Python environment,
**Prepare** for the layout detection model. **Fix _n_ items** at the top runs everything
outstanding in order. Progress appears in the **Output** pane below, and **Copy
diagnostics** puts the whole list on the clipboard for a bug report.

> [!NOTE]
> Creating the Python environment downloads several gigabytes, and preparing
  the layout detection model downloads and converts it. Both take a while on a first run.

The backend cannot start until the Python environment exists.

### Step 3: Review the Settings

**Get started** shows the settings most people change. The **Configuration** screen has
the full list from `config.yaml` and `.proxy-config`, grouped and searchable. Saving a
change while the services are running offers a restart.

> [!NOTE]
> Speaker diarization (identifying who is speaking) is optional and requires a
  one-time Hugging Face access token. The **Get started** screen flags this when
  diarization is enabled without a token; set it under **Configuration**, or see
  [Speaker Diarization Setup](../advanced-setup-guide.md#f-speaker-diarization-setup-optional).

### Step 4: Use the App

The backend starts automatically once setup is complete, unless you passed
`-NoAutoStart`.

| Screen | What it does |
| :--- | :--- |
| **Get started** | Overview of what is ready, what is missing, and the settings most people change |
| **Setup** | Prerequisite checks plus the Python environment and model preparation |
| **Configuration** | Edit `config.yaml` and `.proxy-config` |
| **Services** | Start / stop / restart the backend and watch live logs for it and every child service |

---

## Deprecated: PowerShell Scripts and the Browser UI

> [!WARNING]
> This path is deprecated and will be removed after release 2027.0.0. Use the
  [Desktop App](#desktop-app) instead. `setup-smart-classroom.ps1` and
  `start-smart-classroom.ps1 -WebUI` print a deprecation notice when they run.

Use this path only for unattended installs or when you want the services in visible
console windows.

### Step 1: Run the Setup Script (First-Time Only)

```powershell
.\setup-smart-classroom.ps1
```

> [!NOTE]
> If all prerequisites are already installed (FFmpeg, DL Streamer, Python
  dependencies), you can skip setup and go straight to `.\start-smart-classroom.ps1 -WebUI`.

The setup script will:

1. **[1] Check System Requirements**
   - OS version, CPU, RAM, storage
   - Python and Node.js versions

2. **[2] Application Dependency Check**
   - FFmpeg (auto-install if missing)
   - DL Streamer: auto-download and run the installer [`dlstreamer-2026.1.0-win64.exe`](../advanced-setup-guide.md#b-install-dl-streamer)

3. **[3] Configure Settings**
   - [3.1] Feature Configuration (enable/disable individual application features)
   - [3.2] Language & ASR Configuration (provider, model, device)
   - [3.3] Upload Size Limits
   - [3.4] OCR Configuration
   - [3.5] Board OCR Configuration
   - [3.6] Grading Configuration (enable/disable Smart Grading)

> [!NOTE]
> Speaker diarization (identifying who is speaking) is optional and requires a one-time
  Hugging Face access token setup if enabled — see
  [Speaker Diarization Setup](../advanced-setup-guide.md#f-speaker-diarization-setup-optional).

### Step 2: Start the Services

Use the start script with `-WebUI` for subsequent runs, or after modifying `config.yaml`.
Without `-WebUI` it launches the [Desktop App](#desktop-app) instead.

```powershell
.\start-smart-classroom.ps1 -WebUI
```

**Optional parameters (with `-WebUI`):**

- `-Restart` - Kill existing services and restart without prompting
- `-Silent` - Unattended mode for CI/Ansible (skips all prompts, auto-restarts services)
- `-NoElevate` - Never show a UAC prompt. The services always run as the current user; only enabling Windows long paths needs administrator privileges, and the script requests them for that change alone (never in `-Silent` mode)
- `-NoWindowsTerminal` - Use Invoke-WmiMethod instead of Windows Terminal (for remote sessions/Ansible)

```powershell
# Example: Automated deployment
.\start-smart-classroom.ps1 -WebUI -Silent -NoElevate -NoWindowsTerminal
```

`-Restart` and `-NoWindowsTerminal` are rejected without `-WebUI`, so an existing
unattended command fails with a clear message rather than opening a desktop window.

The startup script performs:

- **Service Detection** - Checks running services
- **Restart Options** - Restart, skip, or abort choices (auto in `-Silent` mode)
- **Proxy Configuration** - Loads from `.proxy-config`
- **Sequential Launch** - Backend -> Content Search -> Grading (if enabled) -> Frontend
- **Graceful Shutdown** - `Q` to stop all, `E` to keep running (auto-exits in `-Silent` mode)

### Step 3: Open the UI

Once all services are running, open your browser:

- <http://localhost:5173>

The UI is only reachable from this machine.

---

## Automated Setup - Troubleshooting

If you encounter issues during automated setup, refer to the manual steps below:

| Issue | Solution |
| :--- | :--- |
| `PSSecurityException` when running `.ps1` scripts | Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in PowerShell |
| FFmpeg installation fails | See [Manual Step 1A](../advanced-setup-guide.md#a-install-ffmpeg-required-for-audio-processing) |
| DL Streamer download fails | See [Manual Step 1B](../advanced-setup-guide.md#b-install-dl-streamer) |
| Python dependencies fail | See [Manual Step 1D](../advanced-setup-guide.md#d-install-python-dependencies) |
| Content Search fails | See [Manual Step 4](../advanced-setup-guide.md#step-4-set-up-content-search) |
| Frontend fails to start | See [Manual Step 6](../advanced-setup-guide.md#step-6-bring-up-the-frontend) |

---

## Manual Setup

**[Advanced Setup Guide](../advanced-setup-guide.md)**: Follow step-by-step instructions to set up the application.

The Advanced Setup Guide covers:

- **Step 1:** Install Dependencies (FFmpeg, DL Streamer, Python, Content Search)
- **Step 2:** Configuration of config.yaml settings, including the optional [Speaker Diarization Setup](../advanced-setup-guide.md#f-speaker-diarization-setup-optional)
- **Steps 3-7:** Run Services and Access UI
- **[Troubleshooting](../advanced-setup-guide.md#troubleshooting)** — solutions for common setup and runtime issues
- **[Known Issues](../advanced-setup-guide.md#known-issues)** — current limitations and workarounds
- **[Uninstall the Application](../advanced-setup-guide.md#uninstall-the-application)** — steps to cleanly remove the environment and models

---

## Service Ports Reference

| Service | Port | Health Check |
| :--- | :--- | :--- |
| Backend | 8000 | <http://localhost:8000/health> |
| Content Search | 9011 | <http://localhost:9011/api/v1/system/health> (200 only when ChromaDB, file ingest, and video preprocess are ready too; 503 while any is starting) |
| Layout Detection | 9902 | <http://localhost:9902/health> |
| Grading | 9012 | <http://localhost:9012/api/v1/health> |
| Frontend (deprecated browser UI) | 5173 | <http://localhost:5173> |

> [!NOTE]
> Layout Detection and Grading services only start when `grading.enabled: true` in `config.yaml`.
> The desktop app is its own frontend and does not use port 5173 (except `-Dev`, which runs the Vite dev server there).

---

## Optional: Flutter + RAG Companion App

Separately from the Smart Classroom app above, the repository also ships a
standalone **Flutter + RAG** demo app — a lightweight Content Search Q&A UI,
independent of Smart Classroom's own frontend. It lives under `utils/flutter`
at the repository root (not under `smart-classroom/Windows`), is Windows-only
(PowerShell `setup.ps1`/`start.ps1`, no Linux equivalents), and the sparse
checkout in [Clone the Repository](#clone-the-repository) does not pull it in
by default.

Its **Agentic/Coding Companion mode** (`/sc-setup`, `/sc-up`, `/sc-doctor`,
`/sc-upload`, `/sc-qa`, `/sc-files`) is driven by skill `.md` files under
`.github/skills`, plus the agent-instructions files every coding assistant
starts from: `.github/copilot-instructions.md` (Copilot), `.cursor/rules/sc.mdc`
(Cursor — itself just a router back to `.github/skills`), and the root-level
`AGENTS.md`/`CLAUDE.md`. All of these are also outside `smart-classroom/Windows`,
so bring `.github` and `.cursor` along too if you want that mode, not just
`utils/flutter` on its own. `AGENTS.md`/`CLAUDE.md` need no extra step — cone-mode
sparse checkout always keeps root-level files regardless of which directories
you set.

To get them too, add their paths to an existing sparse checkout (run from the
`education-ai-suite` repository root):

```bash
git sparse-checkout add utils/flutter .github .cursor
```

Or, if cloning fresh, set all four paths at once:

```bash
git clone --filter=blob:none --sparse --branch main https://github.com/open-edge-platform/education-ai-suite.git
cd education-ai-suite
git sparse-checkout set smart-classroom/Windows utils/flutter .github .cursor
```

For setup, usage, and architecture, see:

- [`utils/flutter/README.md`](../../../../utils/flutter/README.md) — main guide:
  quick start, traditional UI mode, agentic/coding-companion mode,
  architecture, and troubleshooting.
- [`.github/skills/README.md`](../../../../.github/skills/README.md) — the skills
  catalog (`/sc-setup`, `/sc-up`, `/sc-doctor`, `/sc-upload`, `/sc-qa`, `/sc-files`)
  used by the app's Agentic/Coding Companion mode.

## Learn More

- [System Requirements](./system-requirements.md): Hardware, software, supported models, and weight formats.
- [Application Flow](../application-flow.md): End-to-end application flow.
- [Content Search Flow](../content-search-flow.md): The flow of the content search functionality.

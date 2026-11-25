
This folder contains the React UI for the Smart Classroom Application. It runs as a
Windows desktop app (Electron, see [`electron/`](electron/)), which is the default way
to start Smart Classroom.

## Desktop app

End users install the desktop app with `SmartClassroom-<version>-setup.exe` from the
[Releases page](https://github.com/open-edge-platform/education-ai-suite/releases).
From a source checkout, launch it from the repo root of the component:

```powershell
..\start-desktop-app.ps1
```

`..\start-smart-classroom.ps1` without `-WebUI` does the same.

The desktop app **supervises the Python services itself**: it starts and stops
them, tails their logs, edits the configuration files and runs first-time setup.

`start-desktop-app.ps1` bootstraps Node (installing it via `winget` when missing), rebuilds
`dist/`, and hands over. It never requests Administrator privileges.

On the first launch the app opens the **setup assistant**, which checks the PC
and the network, then installs with recommended settings after a short
countdown unless the user chooses **Customize**. When the network test fails or
the disk is nearly full, it waits for the user instead.
On later launches it prepares and starts the backend by itself: when the Python
environment is fine, the backend starts after that one check; when it is
broken, the app runs each repair that needs no Administrator rights and no
confirmation, then starts the backend. Enabling long paths, installing DL
Streamer and **Recreate** always wait for the user, because they raise a UAC
prompt or delete files. Once approved, long paths and DL Streamer run together
in one elevated helper, `Scripts\run_admin_tasks.ps1`, so Windows asks once
and its prompt names Windows PowerShell
([`electron/services/elevation.cjs`](electron/services/elevation.cjs)).
To see the first launch again, delete `state\first-run.json` (`logs\state\` in
a source checkout) and move the Python environment aside.

- `SC_AUTO_START_BACKEND=1` / `0` turns this on or off. The script sets `1`, or
  `0` with `-NoAutoStart`. When the variable is unset, the installed app does it
  and a bare `electron .` does not.
- `-SetupOnly` runs the same preparation without opening a window and exits:
  `0` when everything is ready, `1` when a step failed or the backend still
  cannot start, and `2` when the backend can start but rows remain for the Setup
  screen. For the installed app, run
  `Start-Process "<install dir>\Smart Classroom.exe" -ArgumentList --setup-only -Wait -PassThru`
  and read `ExitCode`. A GUI executable prints nothing to the console, so read the
  `setup-*.log` files under `logs\manager` instead.

| Screen | Backed by |
|--------|-----------|
| **Setup assistant** | [`electron/services/first-run.cjs`](electron/services/first-run.cjs) — the first run only: welcome, network, features, install — [`src/components/assistant`](src/components/assistant) |
| **Setup** | [`electron/services/setup-runner.cjs`](electron/services/setup-runner.cjs) — what is not working, with one **Fix** for what the app can repair ([`src/services/problemList.ts`](src/services/problemList.ts)); below it every check with its actions, and the setup output |
| **Configuration** | [`electron/services/config-store.cjs`](electron/services/config-store.cjs) — comment-preserving writes to `config.yaml`, `.proxy-config` in the user data dir |
| **Services** | [`electron/services/process-manager.cjs`](electron/services/process-manager.cjs) — spawns `main.py`, health-polls it and every service it starts, stops the tree |

### Settings UI

The setup assistant and the Setup, Configuration and Services screens are built
from the same parts, so they look and behave alike and are fixed in one place:

- [`src/components/ui`](src/components/ui): page header, buttons, banners, status
  rows and the log viewer, with one set of colours in `ui.css`.
- [`src/components/setup`](src/components/setup): the sections more than one
  screen shows: setup steps, settings fields with their rule problems, the feature
  picker (feature groups come from `FEATURE_GROUPS` in `config-schema.cjs`), the
  proxy settings with **Test connection**, and the readiness checklist.
- [`src/services/setupStatus.ts`](src/services/setupStatus.ts): what a setup
  step's status means.

A screen composes these and keeps only its own layout: it does not style its own
buttons, banners, rows or headers. A setting appears in the assistant when the
schema gives it an `onboarding` place (a feature group's id, or `network`).

How it reaches the backends: the main API (8000) is CORS-enabled, so calls go to
it directly. Content-search (9011) has no CORS, so like the Vite dev proxy,
the packaged app serves the UI from a local origin and proxies `/api/v1` to
`127.0.0.1:9011` via a small embedded server ([`electron/server.cjs`](electron/server.cjs)).

### Data locations

In the packaged app, writable state lives under `%LOCALAPPDATA%\SmartClassroom\`
(the config copy, `.proxy-config`, models, venvs, `storage/` sessions, logs,
grading and content-search data, and third-party caches for PaddleX, pip and
HuggingFace), so the installed `resources/backend` tree stays read-only and
survives updates.
The backend loads the user's config copy from there via `SC_CONFIG_PATH`; in dev
these paths stay in the source tree.

When an update ships a different `config.yaml` or `components/grading/config.yaml`,
the first start replaces the user's copy with the new default instead of merging
it ([`electron/services/config-reset.cjs`](electron/services/config-reset.cjs)),
because old values can conflict with new keys, combinations or meanings. The
previous copy is saved as `logs\config-backups\pre-update.<file>.<time>.bak` and
the user is told once the window opens. `state\*.sha256` records which shipped
file each copy came from, so an update that leaves a shipped file unchanged keeps
the user's settings.

The uninstaller has an Uninstall Options page with an unchecked box to delete
`%LOCALAPPDATA%\SmartClassroom` as well ([`build/installer.nsh`](build/installer.nsh));
updates and silent uninstalls keep it.

Run the main-process unit tests with `npm run test:electron`.

| Command | What it does |
|---------|--------------|
| `npm run electron:dev` | Runs the Vite dev server + Electron pointed at it (hot reload). |
| `npm run electron:preview` | Builds `dist/` and runs Electron through the production path (embedded static + proxy server) without packaging. |
| `npm run electron:build` | Builds `dist/` and packages a Windows NSIS installer to `release/SmartClassroom-<version>-setup.exe`. |

### Main-process architecture

Everything that touches the OS lives in the main process; the renderer only ever
names things. Two rules make that safe:

- **The renderer cannot supply a command line.** It sends a service id, a config
  path or a step/action id. Each is validated against a static table
  ([`registry.cjs`](electron/services/registry.cjs),
  [`config-schema.cjs`](electron/services/config-schema.cjs),
  [`setup-runner.cjs`](electron/services/setup-runner.cjs)) before anything runs.
- **Every IPC handler checks the sender** is the app's own window, and replies
  with an `{ ok, data } | { ok, error }` envelope carrying a message only — never
  a stack trace. See [`electron/services/ipc.cjs`](electron/services/ipc.cjs).

Secrets never cross the boundary: `models.asr.hf_token` is reported to the UI as
"set" or "not set", and is only written when the user types a replacement.

`contextIsolation`, `sandbox` and `webSecurity` are on; `nodeIntegration` is off.

Model downloads show real progress. The backend and the setup scripts run with
`SC_PROGRESS_EVENTS=1`, and with it
[`utils/download_progress.py`](../utils/download_progress.py) prints a
`@@download {json}` line at most twice a second while a model downloads. The
process manager and the setup runner take those lines out of the log and keep
the latest on the service or the setup step
([`electron/services/download-progress.cjs`](electron/services/download-progress.cjs)),
and the screens draw it with `DownloadBar`.

## Browser UI (deprecated)

> **Deprecated:** the browser UI will be removed after release 2027.0.0. Code that
> exists only for it is tagged `DEPRECATED(webui)`; list it with
> `git grep -n "DEPRECATED(webui)"`.

1. Install **Node 20.19+**
2. `npm install`
3. `npm run dev`, then open <http://localhost:5173>
4. `npm run build` → static files in `dist/` (also used by the desktop app)

## Core dependencies

| Package               | Purpose                                   |
|-----------------------|-------------------------------------------|
| `react` / `react-dom` | UI library and renderer                   |
| `@reduxjs/toolkit`    | Redux store + slices                      |
| `react-redux`         | React bindings for Redux                  |
| `axios`               | HTTP client                               |
| `react-i18next`       | Translations (`src/i18n/`)                |
| `video.js` / `react-player` | Video playback                      |
| `jsmind`              | Mind map rendering                        |
| `pdfjs-dist`          | PDF preview                               |
| `express` / `http-proxy-middleware` | Embedded static + proxy server for the packaged Electron app |
| `yaml`                | Comment-preserving config edits (main process) |

## State & data flow

1. **Redux Toolkit**  
   - Slices: `ui`, `transcript`, `summary`, `mindmap`, `resource`, `classStatistics`,
     `mediaValidation`, `featureConfig`  
   - Typed hooks: `useAppDispatch()` / `useAppSelector()`

2. **Data fetching**  
   - REST calls wrapped in `services/api.ts` (axios + `fetch` for streamed responses)  
   - Long-running pipelines are followed by polling and server-sent events, not sockets  
   - Electron-only bridges live alongside them: `services/serviceManager.ts`,
     `services/configManager.ts`, `services/setupManager.ts`


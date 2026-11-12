
This folder contains the React UI for the Smart Classroom Application. It runs as a
Windows desktop app (Electron, see [`electron/`](electron/)), which is the default way
to start Smart Classroom.

## Desktop app

Launch the desktop app from the repo root of the component:

```powershell
..\start-desktop-app.ps1
```

`..\start-smart-classroom.ps1` without `-WebUI` does the same.

The desktop app **supervises the Python services itself**: it starts and stops
them, tails their logs, edits the configuration files and runs first-time setup.

`start-desktop-app.ps1` bootstraps Node (installing it via `winget` when missing), rebuilds
`dist/`, and hands over. It never requests Administrator privileges.

| Screen | Backed by |
|--------|-----------|
| **Setup** | [`electron/services/setup-runner.cjs`](electron/services/setup-runner.cjs) — prerequisite checks, Python environment, model preparation |
| **Configuration** | [`electron/services/config-store.cjs`](electron/services/config-store.cjs) — comment-preserving writes to `config.yaml`, `.proxy-config` in the user data dir |
| **Services** | [`electron/services/process-manager.cjs`](electron/services/process-manager.cjs) — spawns `main.py`, health-polls it and every service it starts, stops the tree |

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


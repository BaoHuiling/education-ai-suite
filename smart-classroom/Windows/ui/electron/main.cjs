// Electron main process for the Smart Classroom UI.
//
// This layer is purely additive: the same `vite build` output that serves the
// plain web app is loaded here. In dev we point at the running Vite server
// (which already proxies /api/v1); when packaged we serve `dist/` through the
// embedded static + proxy micro-server (server.cjs).
//
// The Python backends can either be started externally (legacy PowerShell
// path, detected and attached to) or supervised by this process — see
// electron/services/.

const fs = require('fs');
const path = require('path');
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');
const { startServer } = require('./server.cjs');
const servicePaths = require('./services/paths.cjs');
const serviceIpc = require('./services/ipc.cjs');
const { LogStore } = require('./services/log-store.cjs');
const { ServiceManager } = require('./services/process-manager.cjs');
const { SetupRunner, LOG_ID: SETUP_LOG_ID } = require('./services/setup-runner.cjs');
const winEnv = require('./services/win-env.cjs');
const { resetToShippedDefaults } = require('./services/config-reset.cjs');

const configResets = [];
if (servicePaths.ensureUserData()) {
  const targets = [
    { label: 'config.yaml', userFile: servicePaths.configFile(), defaultFile: servicePaths.bundledConfigFile() },
    {
      label: 'grading-config.yaml',
      userFile: servicePaths.gradingConfigFile(),
      defaultFile: servicePaths.bundledGradingConfigFile(),
    },
  ];
  for (const target of targets) {
    const result = resetToShippedDefaults({
      ...target,
      stampFile: servicePaths.shippedConfigStampFile(target.label),
      backupDir: servicePaths.configBackupDir(),
    });
    if (result.status === 'reset') {
      console.info(`[config] ${target.userFile} reset to the shipped defaults; previous copy: ${result.backup}`);
      configResets.push({ file: target.userFile, backup: result.backup });
    } else if (result.status === 'failed') {
      console.warn(`[config] could not check ${target.userFile} against the shipped defaults: ${result.error}`);
    }
  }
}

// Height (px) of the custom title bar strip. Matches the TopPanel so the
// native Window Controls Overlay buttons align with the app header.
const TITLE_BAR_HEIGHT = 63;

// The Window Controls Overlay strip is painted by OS, not by the page, so a
// DOM overlay cannot dim it. The renderer reports which surface is on top and we
// recolour the caption to match.
const TITLE_BAR_THEMES = {
  // App header.
  default: { color: '#0071c5', symbolColor: '#ffffff' },
  // Brand blue and white composited under 50% black. Used by every surface that
  // covers the caption: the modals, and the slide-over panels — those start
  // below the caption strip, so what reaches it is their backdrop, not the
  // panel itself.
  dimmed: { color: '#003862', symbolColor: '#7f7f7f' },
};

// The overlay height is in device-independent pixels, but the TopPanel it must
// line up with is in CSS pixels, which page zoom (Ctrl+-/Ctrl+=/Ctrl+wheel)
// rescales. Left at a fixed height, zooming out shrinks the header under the
// native controls and they hang over the header bar below it. So the overlay is
// re-applied at TITLE_BAR_HEIGHT x zoom factor, remembering per window the
// theme the renderer last asked for and the height last applied. Rounded down:
// the height is whole DIPs, and rounding up lets the overlay cover the header's
// bottom border by a device pixel (visible at 150% display scaling), while a
// short overlay is invisible against the header's identical blue.
const titleBarState = new WeakMap();

function applyTitleBarOverlay(win, theme) {
  if (process.platform === 'darwin' || !win || win.isDestroyed()) return;
  const state = titleBarState.get(win) || { theme: 'default', height: 0 };
  const nextTheme = theme || state.theme;
  const height = Math.floor(TITLE_BAR_HEIGHT * win.webContents.getZoomFactor());
  if (nextTheme === state.theme && height === state.height) return;
  const colors = TITLE_BAR_THEMES[nextTheme] || TITLE_BAR_THEMES.default;
  try {
    win.setTitleBarOverlay({ ...colors, height });
    titleBarState.set(win, { theme: nextTheme, height });
  } catch {
    // Overlay unavailable on this platform/frame — leave the caption alone.
  }
}

// ---------------------------------------------------------------------------
// Native-menu localization
// ---------------------------------------------------------------------------
// The native application menu and right-click context menu are rendered by the
// OS shell, not React, so react-i18next cannot reach them. We keep a small label
// table here (mirroring src/i18n) and rebuild the menus when the renderer reports
// a language change. Roles are preserved for native behavior/accelerators; only
// the `label` is translated. Unknown languages fall back to English.
const MENU_LABELS = {
  en: {
    file: 'File', edit: 'Edit', view: 'View', window: 'Window',
    quit: 'Quit', close: 'Close',
    undo: 'Undo', redo: 'Redo', cut: 'Cut', copy: 'Copy', paste: 'Paste', selectAll: 'Select All',
    reload: 'Reload', forceReload: 'Force Reload', toggleDevTools: 'Toggle Developer Tools',
    resetZoom: 'Actual Size', zoomIn: 'Zoom In', zoomOut: 'Zoom Out', togglefullscreen: 'Toggle Full Screen',
    minimize: 'Minimize', zoom: 'Zoom',
  },
  zh: {
    file: '文件', edit: '编辑', view: '视图', window: '窗口',
    quit: '退出', close: '关闭',
    undo: '撤销', redo: '重做', cut: '剪切', copy: '复制', paste: '粘贴', selectAll: '全选',
    reload: '重新加载', forceReload: '强制重新加载', toggleDevTools: '切换开发者工具',
    resetZoom: '实际大小', zoomIn: '放大', zoomOut: '缩小', togglefullscreen: '切换全屏',
    minimize: '最小化', zoom: '缩放',
  },
};

// Current native-menu language; updated via the 'menu:setLanguage' IPC channel.
let currentLanguage = 'en';

function menuLabels(lang) {
  return MENU_LABELS[lang] || MENU_LABELS.en;
}

// Build an explicit application menu (File / Edit / View / Window) with
// translated labels. Standard roles preserve native behavior and accelerators.
function buildAppMenu(lang = currentLanguage) {
  const L = menuLabels(lang);
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: L.file,
      submenu: [{ role: isMac ? 'close' : 'quit', label: isMac ? L.close : L.quit }],
    },
    {
      label: L.edit,
      submenu: [
        { role: 'undo', label: L.undo },
        { role: 'redo', label: L.redo },
        { type: 'separator' },
        { role: 'cut', label: L.cut },
        { role: 'copy', label: L.copy },
        { role: 'paste', label: L.paste },
        { role: 'selectAll', label: L.selectAll },
      ],
    },
    {
      label: L.view,
      submenu: [
        { role: 'reload', label: L.reload },
        { role: 'forceReload', label: L.forceReload },
        { role: 'toggleDevTools', label: L.toggleDevTools },
        { type: 'separator' },
        { role: 'resetZoom', label: L.resetZoom },
        { role: 'zoomIn', label: L.zoomIn },
        { role: 'zoomOut', label: L.zoomOut },
        { type: 'separator' },
        { role: 'togglefullscreen', label: L.togglefullscreen },
      ],
    },
    {
      label: L.window,
      submenu: [
        { role: 'minimize', label: L.minimize },
        { role: 'zoom', label: L.zoom },
        ...(isMac ? [] : [{ role: 'close', label: L.close }]),
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

// Build a right-click context menu with basic text operations, tailored to the
// clicked element and translated to `lang`. Returns null when there is nothing
// useful to show. `params` is the object from the webContents 'context-menu' event.
function buildContextMenu(params, lang = currentLanguage) {
  const L = menuLabels(lang);
  const { editFlags, isEditable, selectionText } = params;
  const hasSelection = selectionText.trim().length > 0;
  const template = [];

  if (isEditable) {
    template.push(
      { role: 'undo', label: L.undo, enabled: editFlags.canUndo },
      { role: 'redo', label: L.redo, enabled: editFlags.canRedo },
      { type: 'separator' },
      { role: 'cut', label: L.cut, enabled: editFlags.canCut },
      { role: 'copy', label: L.copy, enabled: editFlags.canCopy },
      { role: 'paste', label: L.paste, enabled: editFlags.canPaste },
      { type: 'separator' },
      { role: 'selectAll', label: L.selectAll, enabled: editFlags.canSelectAll }
    );
  } else if (hasSelection) {
    template.push(
      { role: 'copy', label: L.copy, enabled: editFlags.canCopy },
      { type: 'separator' },
      { role: 'selectAll', label: L.selectAll, enabled: editFlags.canSelectAll }
    );
  } else {
    template.push(
      { role: 'reload', label: L.reload },
      { role: 'toggleDevTools', label: L.toggleDevTools }
    );
  }

  return template.length ? Menu.buildFromTemplate(template) : null;
}

// Vite dev server URL, set by the `electron:dev` script. Absent when packaged.
const DEV_SERVER_URL = process.env.ELECTRON_START_URL;

let mainWindow = null;
let serverHandle = null;
const logStore = new LogStore(servicePaths.managerLogDir());
const serviceManager = new ServiceManager(logStore);
const setupRunner = new SetupRunner(logStore);

serviceManager.setStartGuard(() =>
  setupRunner.working
    ? { code: 'setup', message: 'Setup is still preparing the Python environment. Start the backend when it finishes.' }
    : null
);

// The service snapshot is pushed only when a status changes, so the renderer's
// `runnable` (the interpreter exists) goes stale while setup creates or
// removes the environment. Push it again whenever setup starts or stops work.
let setupWasWorking = false;
setupRunner.on('changed', () => {
  if (setupRunner.working === setupWasWorking) return;
  setupWasWorking = setupRunner.working;
  serviceManager.emitChanged();
});

// `--setup-only`: prepare the machine without opening a window, print what was
// done, and exit with a code a script can test. Used by
// start-desktop-app.ps1 -SetupOnly and by unattended validation.
const SETUP_ONLY = process.argv.includes('--setup-only');

const SETUP_EXIT = {
  // Every step is satisfied.
  READY: 0,
  // An automatic step failed, was skipped or cancelled, or the backend still
  // cannot start.
  FAILED: 1,
  // The backend can start, but rows remain that need an administrator prompt or
  // a confirmed click on the Setup screen (long paths, DL Streamer, Recreate).
  NEEDS_ATTENTION: 2,
};

/**
 * Whether launching the app prepares the environment and starts the backend
 * without being asked.
 *
 * SC_AUTO_START_BACKEND is set by start-desktop-app.ps1: '1' by default and '0'
 * for -NoAutoStart. When nothing set it, the installed app does this and a bare
 * `electron .` from a developer's checkout does not. The installed app opts in
 * because "install, then click through Setup before anything works" is the
 * first-run experience this replaces.
 */
function autoPrepareEnabled() {
  const flag = process.env.SC_AUTO_START_BACKEND;
  if (flag === '1') return true;
  if (flag === '0') return false;
  return app.isPackaged;
}

const backendService = () => serviceManager.snapshot().find((service) => service.id === 'backend');
const isIdle = (service) => service?.status === 'stopped' || service?.status === 'failed';

/**
 * Startup sequence for the windowed app: check everything, then, if allowed,
 * run every repair that needs no one at the keyboard and start the backend.
 *
 * Progress shows on the Get started and Setup screens like a Fix run would,
 * because it uses the same runner. A first launch on a fresh machine therefore
 * ends with a running backend instead of an idle app waiting for clicks.
 */
async function prepareAndStartBackend() {
  const startBackend = () => {
    // Re-read: setup may have created the environment, and the user may have
    // started the backend from Services in the meantime.
    const backend = backendService();
    if (!backend?.runnable || !isIdle(backend)) return;
    serviceManager.startService('backend').catch((error) => {
      console.error('[electron] backend auto-start failed:', error.message);
    });
  };

  // checkAll() is unconditional: it fills in the Setup screen so a first-time
  // user sees real statuses instead of "Not checked".
  if (!autoPrepareEnabled()) {
    await setupRunner.checkAll();
    return;
  }

  // Every launch after the first: the environment check alone says whether the
  // backend can start, so start it now rather than after the driver queries.
  // An environment that exists but is incomplete (a first run closed during
  // pip) fails this and takes the setup path below. Outdated still starts, as
  // it always has; Upgrade stays on the Setup screen.
  if (backendService()?.runnable) {
    const venv = await setupRunner.checkStep('venv');
    if (venv.status === 'ok' || venv.status === 'outdated') {
      startBackend();
      await setupRunner.checkAll();
      return;
    }
  }

  await setupRunner.checkAll();
  // A backend already up, for example one adopted from the legacy PowerShell
  // path, has the environment's files in use, so setup cannot change them.
  if (!isIdle(backendService())) return;

  const result = await setupRunner.runAutomatic();
  if (result.ran.length || result.failed.length) {
    console.info(
      `[setup] automatic run: ${result.ran.length} ran, ${result.failed.length} failed, ` +
      `${result.blocked.length} blocked, ${result.manual.length} left for the Setup screen`
    );
  }

  // A cancel means the user has taken over.
  if (setupRunner.autoCancelled) return;
  startBackend();
}

/** The `--setup-only` run. Resolves to the process exit code. */
async function runSetupOnly() {
  const print = (line) => process.stdout.write(`${line}\n`);

  // The same lines the Setup screen's Output pane shows.
  logStore.on('append', (batch) => {
    if (batch.id !== SETUP_LOG_ID) return;
    for (const line of batch.lines) print(line.text);
  });

  print('Checking prerequisites...');
  await setupRunner.checkAll();
  const result = await setupRunner.runAutomatic();
  const steps = setupRunner.snapshot();

  print('');
  print('Setup summary:');
  for (const step of steps) {
    print(`  ${step.status.padEnd(9)} ${step.label}${step.optional ? ' (optional)' : ''}: ${step.detail}`);
  }

  const label = (id) => steps.find((step) => step.id === id)?.label ?? id;
  const needsWork = steps.filter((step) => ['missing', 'failed', 'outdated'].includes(step.status));
  const runnable = !!backendService()?.runnable;

  print('');
  if (result.manual.length) {
    print(`Left for the Setup screen (administrator prompt or confirmation): ${result.manual.map(label).join(', ')}`);
  }
  if (result.blocked.length) {
    print(`Skipped because a prerequisite is not ready: ${result.blocked.map(label).join(', ')}`);
  }
  print(`Setup log folder: ${servicePaths.managerLogDir()}`);

  if (setupRunner.autoCancelled || result.failed.length || result.blocked.length || !runnable) {
    print(runnable ? 'Setup did not complete.' : 'Setup did not complete: the backend cannot start yet.');
    return SETUP_EXIT.FAILED;
  }
  if (needsWork.length) {
    print('The backend is ready to start. Some items still need attention on the Setup screen.');
    return SETUP_EXIT.NEEDS_ATTENTION;
  }
  print('Setup complete. The backend is ready to start.');
  return SETUP_EXIT.READY;
}

async function resolveStartUrl() {
  if (DEV_SERVER_URL) return DEV_SERVER_URL;
  // Resolve `dist/` relative to this file (ui/electron/main.cjs -> ui/dist).
  const distPath = path.join(__dirname, '..', 'dist');
  serverHandle = await startServer(distPath);
  return `http://127.0.0.1:${serverHandle.port}`;
}

function reportConfigResets(win) {
  if (!configResets.length) return;
  const resets = configResets.splice(0);
  dialog
    .showMessageBox(win, {
      type: 'info',
      title: 'Smart Classroom',
      message: 'Settings were reset to the defaults of this version.',
      detail:
        'This version ships new default settings, so your previous configuration was replaced. ' +
        'Review your settings, including the Hugging Face token, under Configuration.\n\n' +
        `Your previous files were saved as:\n${resets.map((reset) => reset.backup).join('\n')}`,
      buttons: ['OK', 'Show previous files'],
      defaultId: 0,
      cancelId: 0,
    })
    .then(({ response }) => {
      if (response === 1) shell.showItemInFolder(resets[0].backup);
    })
    .catch(() => { });
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 1000,
    minHeight: 600,
    show: false,
    title: 'Smart Classroom',
    titleBarStyle: 'hidden',
    ...(process.platform !== 'darwin'
      ? {
        titleBarOverlay: {
          color: '#0071c5',
          symbolColor: '#ffffff',
          height: TITLE_BAR_HEIGHT,
        },
      }
      : {}),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  // Open http(s) links (e.g. external links) in the OS browser
  // rather than inside the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  // The zoomIn menu role is bound to Ctrl+Plus, which on a US layout means
  // Ctrl+Shift+=, so the unshifted Ctrl+= and numpad + did nothing while
  // Ctrl+- and Ctrl+0 worked. Handle every zoom-in key here, mirroring the
  // role's 0.5 zoom-level step; preventDefault also stops the menu
  // accelerator, so Ctrl+Shift+= still zooms only once.
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown' || input.alt || !(input.control || input.meta)) return;
    if (input.key !== '=' && input.key !== '+') return;
    event.preventDefault();
    mainWindow.webContents.zoomLevel += 0.5;
  });

  // Right-click context menu with basic text operations.
  mainWindow.webContents.on('context-menu', (_event, params) => {
    const menu = buildContextMenu(params);
    if (menu) menu.popup({ window: mainWindow });
  });

  // A dead renderer process cannot be caught by the in-page error boundary, so
  // report it natively rather than leaving a blank window.
  mainWindow.webContents.on('render-process-gone', async (_event, details) => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: 'error',
      title: 'Smart Classroom',
      message: 'The interface process stopped unexpectedly.',
      detail: `Reason: ${details.reason}${Number.isInteger(details.exitCode) ? ` (exit code ${details.exitCode})` : ''
        }\n\nBackend services are unaffected and keep running.`,
      buttons: ['Reload', 'Quit'],
      defaultId: 0,
      cancelId: 1,
    });
    if (response === 0) mainWindow.reload();
    else app.quit();
  });

  mainWindow.webContents.on('preload-error', (_event, preloadPath, error) => {
    console.error('[electron] preload script failed:', preloadPath, error);
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
    reportConfigResets(mainWindow);
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  const startUrl = await resolveStartUrl();
  await mainWindow.loadURL(startUrl);
}

// Single-instance: focus the existing window instead of opening a second one.
// A --setup-only run needs the lock too, because an open window may be running
// setup or the backend on the same environment.
if (!app.requestSingleInstanceLock()) {
  if (SETUP_ONLY) {
    process.stderr.write('Smart Classroom is already running. Close it, then run setup again.\n');
    app.exit(SETUP_EXIT.FAILED);
  } else {
    app.quit();
  }
} else if (SETUP_ONLY) {
  app.whenReady().then(async () => {
    let code = SETUP_EXIT.FAILED;
    try {
      code = await runSetupOnly();
    } catch (error) {
      process.stderr.write(`Setup failed: ${error.message}\n`);
    }
    await logStore.disposeAndFlush().catch(() => { });
    app.exit(code);
  });
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  // Renderer reports the surface currently covering the caption area (a modal,
  // the report panel, or nothing) so the overlay can be recoloured to match.
  ipcMain.on('titlebar:setTheme', (event, theme) => {
    const name = Object.hasOwn(TITLE_BAR_THEMES, theme) ? theme : 'default';
    applyTitleBarOverlay(BrowserWindow.fromWebContents(event.sender), name);
  });

  // Renderer's viewport changed size, which is also how a page-zoom change
  // shows up there; rescale the overlay if the zoom factor moved.
  ipcMain.on('titlebar:syncZoom', (event) => {
    applyTitleBarOverlay(BrowserWindow.fromWebContents(event.sender));
  });

  // Renderer reports its active language; rebuild the native application menu
  // in that language. The context menu reads `currentLanguage` at popup time,
  // so it needs no rebuild here.
  ipcMain.on('menu:setLanguage', (_event, lang) => {
    if (typeof lang !== 'string' || !lang) return;
    currentLanguage = lang;
    Menu.setApplicationMenu(buildAppMenu(currentLanguage));
  });

  // Open the OS-native file chooser (multi-select) and return the chosen files as
  // { path, name, size } records — size is read here so the renderer can show it in
  // the staging table without ever holding the file contents. Empty when cancelled.
  ipcMain.handle('dialog:pickFiles', async (event, options) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    const extensions = Array.isArray(options?.extensions) ? options.extensions : [];
    const dialogOptions = {
      properties: ['openFile', 'multiSelections'],
      ...(extensions.length ? { filters: [{ name: 'Supported files', extensions }] } : {}),
      ...(typeof options?.defaultPath === 'string' && options.defaultPath
        ? { defaultPath: options.defaultPath }
        : {}),
    };
    const result = win
      ? await dialog.showOpenDialog(win, dialogOptions)
      : await dialog.showOpenDialog(dialogOptions);
    if (result.canceled || !result.filePaths.length) return [];
    return result.filePaths.map((filePath) => {
      let size = 0;
      try {
        size = fs.statSync(filePath).size;
      } catch {
        // Unreadable file: report size 0 and let the backend reject it on ingest.
      }
      return { path: filePath, name: path.basename(filePath), size };
    });
  });

  // Open the OS-native folder chooser and return the selected absolute path
  // ('' when cancelled).
  ipcMain.handle('dialog:pickDirectory', async (event, defaultPath) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    const options = {
      properties: ['openDirectory', 'createDirectory'],
      ...(typeof defaultPath === 'string' && defaultPath ? { defaultPath } : {}),
    };
    const result = win
      ? await dialog.showOpenDialog(win, options)
      : await dialog.showOpenDialog(options);
    if (result.canceled || !result.filePaths.length) return '';
    return result.filePaths[0];
  });

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(buildAppMenu());

    // Before anything is spawned: children inherit this process's environment,
    // so a tool installed since the last run — or by the Setup screen in a
    // previous session — would otherwise be invisible to the backend too.
    await winEnv.refreshPath().catch(() => []);
    await winEnv.applyDlStreamerEnv().catch(() => null);

    serviceIpc.register({
      manager: serviceManager,
      logs: logStore,
      setup: setupRunner,
      getWindow: () => mainWindow,
      getLanguage: () => currentLanguage,
    });
    serviceManager.start();

    prepareAndStartBackend().catch((error) => {
      console.error('[electron] automatic setup or backend start failed:', error.message);
    });

    // A backend that dies on a missing package leaves Setup insisting the
    // environment is fine. Re-check on the transition into failed, so the two
    // screens stop disagreeing. Edge-triggered: a service that stays failed must
    // not re-check on every health tick.
    let backendFailed = false;
    serviceManager.on('changed', (snapshot) => {
      const backend = snapshot.find((service) => service.id === 'backend');
      const failed = backend?.status === 'failed';
      if (failed && !backendFailed) setupRunner.checkAll().catch(() => { });
      backendFailed = failed;
    });

    // Open the native application menu as a popup, positioned under the
    // title-bar menu button (coordinates come from the renderer, in viewport
    // pixels which map to the frameless window's content area).
    ipcMain.on('menu:popup', (event, position) => {
      const menu = Menu.getApplicationMenu();
      if (!menu) return;
      const win = BrowserWindow.fromWebContents(event.sender);
      const opts = win ? { window: win } : {};
      if (position && Number.isFinite(position.x) && Number.isFinite(position.y)) {
        opts.x = Math.round(position.x);
        opts.y = Math.round(position.y);
      }
      menu.popup(opts);
    });

    createWindow();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
}

app.on('window-all-closed', () => {
  if (serverHandle) serverHandle.close();
  if (process.platform !== 'darwin') app.quit();
});

// Tear down only the processes this app started; externally started backends
// (legacy PowerShell path) are left running.
let shuttingDown = false;
app.on('before-quit', (event) => {
  if (shuttingDown) return;
  shuttingDown = true;
  event.preventDefault();
  serviceManager
    .shutdown()
    .catch(() => { })
    .finally(() => {
      logStore.dispose();
      app.quit();
    });
});

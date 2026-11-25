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
const { app, BrowserWindow, Menu, Notification, dialog, ipcMain, powerSaveBlocker, session, shell } = require('electron');
const { startServer } = require('./server.cjs');
const servicePaths = require('./services/paths.cjs');
const serviceIpc = require('./services/ipc.cjs');
const { LogStore } = require('./services/log-store.cjs');
const { ServiceManager } = require('./services/process-manager.cjs');
const { SetupRunner, LOG_ID: SETUP_LOG_ID } = require('./services/setup-runner.cjs');
const { FirstRunController } = require('./services/first-run.cjs');
const networkProbe = require('./services/network-probe.cjs');
const config = require('./services/config-store.cjs');
const preferences = require('./services/preferences.cjs');
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
// Setup downloads gigabytes; a PC that sleeps half-way through stalls it.
let powerBlocker = null;
setupRunner.on('changed', () => {
  if (setupRunner.working === setupWasWorking) return;
  setupWasWorking = setupRunner.working;
  serviceManager.emitChanged();
  if (setupWasWorking && powerBlocker === null) {
    powerBlocker = powerSaveBlocker.start('prevent-app-suspension');
  } else if (!setupWasWorking && powerBlocker !== null) {
    powerSaveBlocker.stop(powerBlocker);
    powerBlocker = null;
  }
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

/** Start the backend if it can start and nobody has started it yet. */
function startBackend() {
  // Re-read: setup may have created the environment, and the user may have
  // started the backend from Services in the meantime.
  const backend = backendService();
  if (!backend?.runnable || !isIdle(backend)) return;
  serviceManager.startService('backend').catch((error) => {
    console.error('[electron] backend auto-start failed:', error.message);
  });
}

const preferencesFile = () => path.join(servicePaths.stateDir(), 'preferences.json');

const configuredHub = () => (config.readConfig()?.models?.model_hub === 'modelscope' ? 'modelscope' : 'huggingface');

// The first-run wizard's flow. It runs only when launchPlan() says `wizard`;
// the window shows it and sends choices, and its countdown runs here, so a
// window that never loads cannot stall it.
const firstRun = new FirstRunController({
  recordFile: () => path.join(servicePaths.stateDir(), 'first-run.json'),
  setupRunner,
  testNetwork: () =>
    networkProbe.test({
      saved: networkProbe.readSaved(),
      env: networkProbe.readEnv(process.env),
      // The Windows proxy settings, PAC scripts included, as Chromium resolves
      // them; pip and the model downloads cannot read PAC scripts themselves.
      resolveSystem: (url) => session.defaultSession.resolveProxy(url),
    }),
  configuredHub,
  applyChanges: (changes) => config.apply(changes),
  startBackend,
  // Into the Setup screen's Output pane, so what was decided is visible.
  log: (line) => logStore.note(SETUP_LOG_ID, line),
});

// ---------------------------------------------------------------------------
// First-run installation in the background
// ---------------------------------------------------------------------------
// The installation runs for a long time, often with the window behind others
// or minimized, so it also reports through the taskbar and a notification.
// Native, out of react-i18next's reach, hence the text tables here.

const NOTIFY_TEXT = {
  en: {
    ready: 'Setup is finished. Smart Classroom is ready.',
    failed: 'Setup could not finish. Open Smart Classroom to see why.',
    startFailed: 'Setup is finished, but the backend did not start. Open Smart Classroom to see why.',
  },
  zh: {
    ready: '安装完成，Smart Classroom 已就绪。',
    failed: '安装未能完成。请打开 Smart Classroom 查看原因。',
    startFailed: '安装已完成，但后端未能启动。请打开 Smart Classroom 查看原因。',
  },
};

const CLOSE_TEXT = {
  en: {
    message: 'Setup is still running.',
    resumes: 'Closing stops it. It continues the next time you open Smart Classroom.',
    rerun: 'Closing stops it. You can run it again from the Setup screen.',
    keep: 'Keep running',
    close: 'Close',
  },
  zh: {
    message: '安装仍在进行中。',
    resumes: '关闭将停止安装，下次打开 Smart Classroom 时会继续。',
    rerun: '关闭将停止安装，之后可以在“安装”页面重新运行。',
    keep: '继续运行',
    close: '关闭',
  },
};

let notified = null;

function notifyFirstRun(kind) {
  if (notified === kind) return;
  notified = kind;
  if (!Notification.isSupported() || mainWindow?.isFocused()) return;
  const notification = new Notification({
    title: 'Smart Classroom',
    body: (NOTIFY_TEXT[currentLanguage] ?? NOTIFY_TEXT.en)[kind],
  });
  notification.on('click', () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });
  notification.show();
}

/** Taskbar progress and the closing notification, from the flow's state. */
function reportFirstRun() {
  const flow = firstRun.snapshot();
  if (!flow.active) return;
  const backend = backendService();
  const setProgress = (value, mode) => {
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setProgressBar(value, mode ? { mode } : undefined);
  };

  if (flow.phase === 'installing') {
    // A retry deserves its own notification at the end.
    notified = null;
    const runnable = (flow.plan ?? []).filter(
      (entry) => entry.kind === 'auto' || (entry.kind === 'elevated' && flow.includeElevated)
    );
    const steps = setupRunner.snapshot();
    const done = runnable.filter((entry) => {
      const status = steps.find((step) => step.id === entry.stepId)?.status;
      return status === 'ok' || status === 'warn';
    }).length;
    // The backend's first start is the last item, so setup alone never fills
    // the bar.
    if (runnable.length) setProgress(done / (runnable.length + 1));
    else setProgress(1, 'indeterminate');
    return;
  }

  if (flow.phase === 'finished' && !flow.result?.envOk) {
    setProgress(1, 'error');
    notifyFirstRun('failed');
    return;
  }

  if (flow.phase === 'finished') {
    if (backend?.status === 'starting') {
      setProgress(1, 'indeterminate');
    } else if (backend?.status === 'healthy') {
      setProgress(-1);
      notifyFirstRun('ready');
    } else if (backend?.status === 'failed') {
      setProgress(1, 'error');
      notifyFirstRun('startFailed');
    }
    return;
  }

  setProgress(-1);
}

firstRun.on('changed', reportFirstRun);
setupRunner.on('changed', reportFirstRun);
serviceManager.on('changed', reportFirstRun);

/**
 * Closing the window mid-setup stops it, so ask first. The first-run
 * installation resumes at the next launch; a step started from the Setup
 * screen has to be run again.
 */
function confirmCloseDuringSetup(window) {
  let confirmed = false;
  window.on('close', (event) => {
    if (confirmed || !setupRunner.working) return;
    event.preventDefault();
    const text = CLOSE_TEXT[currentLanguage] ?? CLOSE_TEXT.en;
    const resumes = firstRun.snapshot().phase === 'installing';
    dialog
      .showMessageBox(window, {
        type: 'warning',
        title: 'Smart Classroom',
        message: text.message,
        detail: resumes ? text.resumes : text.rerun,
        buttons: [text.keep, text.close],
        defaultId: 0,
        cancelId: 0,
      })
      .then(({ response }) => {
        if (response !== 1 || window.isDestroyed()) return;
        confirmed = true;
        firstRun.stopForExit();
        setupRunner.cancel();
        window.close();
      })
      .catch(() => { });
  });
}

/**
 * Startup sequence for the windowed app.
 *
 * - The Python environment is OK: start the backend, then check the rest.
 * - Never set up: the first-run wizard's flow, which installs by itself after
 *   a countdown, or at once when no wizard is on screen.
 * - Set up before, and broken since: the automatic repair, which runs every
 *   repair that needs no one at the keyboard, then starts the backend.
 * - The user chose to set up by hand: check only.
 */
async function prepareAndStartBackend() {
  // The environment check alone says whether the backend can start, so a
  // returning user's backend starts now rather than after the driver queries.
  // An environment that exists but is incomplete (a first run closed during
  // pip) fails it. Outdated still starts, as it always has; Upgrade stays on
  // the Setup screen.
  let envOk = false;
  if (backendService()?.runnable) {
    const venv = await setupRunner.checkStep('venv');
    envOk = venv.status === 'ok' || venv.status === 'outdated';
  }

  const plan = firstRun.launchPlan({ envOk });

  if (plan === 'ready') {
    // An upgrade from a version without the wizard, or a machine prepared by
    // --setup-only: the wizard has nothing to do there.
    firstRun.markCompleted();
    if (autoPrepareEnabled()) startBackend();
    // Unconditional: it fills in the Setup screen so a first-time user sees
    // real statuses instead of "Not checked".
    await setupRunner.checkAll();
    return;
  }

  // A backend already up, for example one adopted from the legacy PowerShell
  // path, has the environment's files in use, so setup cannot change them.
  if (!isIdle(backendService())) {
    await setupRunner.checkAll();
    return;
  }

  if (plan === 'wizard') {
    // The flow runs the checks itself, alongside the network test.
    firstRun.start({ autoStart: autoPrepareEnabled() });
    return;
  }

  await setupRunner.checkAll();
  if (plan === 'manual' || !autoPrepareEnabled()) return;

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

  confirmCloseDuringSetup(mainWindow);

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

  // The UI language to start in, read synchronously by the preload so the
  // first render is already in it: the last explicit choice, else the first
  // supported language Windows prefers.
  ipcMain.on('app:initialLanguage', (event) => {
    event.returnValue = preferences.pickLanguage(
      preferences.readPreferences(preferencesFile()).language,
      app.getPreferredSystemLanguages()
    );
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
      firstRun,
      saveLanguage: (language) => preferences.saveLanguage(preferencesFile(), language),
      network: {
        probeEntered: (values) =>
          networkProbe.probeEntered(values, {
            hub: configuredHub(),
            needInstallers: networkProbe.needsInstallers(setupRunner.plan()),
          }),
      },
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

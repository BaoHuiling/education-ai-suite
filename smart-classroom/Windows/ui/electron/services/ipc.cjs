// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// IPC surface for service management.
//
// Everything crossing this boundary is validated: the sender must be the app's
// own window, and the only accepted argument is a service id that exists in the
// registry. The renderer can never supply a command, path or port.

const { ipcMain, shell, dialog, clipboard } = require('electron');
const config = require('./config-store.cjs');
const paths = require('./paths.cjs');
const registry = require('./registry.cjs');
const { LOG_ID: setupLogId } = require('./setup-runner.cjs');

function ok(data) {
  return { ok: true, data };
}

function fail(error) {
  // Message only — stacks and absolute paths stay in the main process log.
  // A code comes too when the thrower set one, so the renderer can offer a way
  // out of a known failure without matching on its English text — and params
  // with it, so the renderer can also say it in the UI language.
  const code = typeof error?.code === 'string' ? error.code : undefined;
  const params = code && error?.params && typeof error.params === 'object' ? error.params : undefined;
  return { ok: false, error: error instanceof Error ? error.message : String(error), code, params };
}

// The Recreate confirmation is a native dialog, out of react-i18next's reach,
// so its text lives here like the menu labels in main.cjs. Recreate on the two
// environments is the only destructive action, hence the short name tables.
const CONFIRM_TEXT = {
  en: {
    message: (action, step) => `${action.label} the ${step.label.toLowerCase()}?`,
    deletesFolder: (folder) =>
      `${folder}\n\nThis folder will be deleted and every package reinstalled, which takes several minutes and needs network access.`,
    deletesAll: 'Everything it contains will be deleted and reinstalled, which takes several minutes and needs network access.',
    cancel: 'Cancel',
    confirm: (action) => `${action.label} and reinstall`,
  },
  zh: {
    message: (action, step) =>
      `${{ recreate: '重新创建' }[action.id] ?? action.label}${{ venv: 'Python 环境', gradingVenv: '评分转换环境' }[step.id] ?? step.label}？`,
    deletesFolder: (folder) => `${folder}\n\n将删除此文件夹并重新安装所有软件包，需要几分钟时间且需要联网。`,
    deletesAll: '其中的所有内容都将被删除并重新安装，需要几分钟时间且需要联网。',
    cancel: '取消',
    confirm: () => '重新创建并安装',
  },
};

function register({ manager, logs, setup, getWindow, getLanguage = () => 'en' }) {
  const trusted = (event) => {
    const window = getWindow();
    return !!window && !window.isDestroyed() && event.sender.id === window.webContents.id;
  };

  const serviceId = (value) => {
    if (!registry.get(value)) throw new Error(`Unknown service: ${String(value).slice(0, 64)}`);
    return value;
  };

  const handle = (channel, handler) => {
    ipcMain.handle(channel, async (event, ...args) => {
      if (!trusted(event)) return fail('Rejected: untrusted sender.');
      try {
        return ok(await handler(...args));
      } catch (error) {
        return fail(error);
      }
    });
  };

  handle('services:list', () => manager.snapshot());
  handle('services:start', (id) => manager.startService(serviceId(id)));
  handle('services:stop', (id) => manager.stopService(serviceId(id)));
  handle('services:restart', (id) => manager.restartService(serviceId(id)));

  handle('logs:read', (id, options) => {
    const limit = Math.min(Math.max(Number(options?.limit) || 1000, 1), 5000);
    const sinceSeq = Math.max(Number(options?.sinceSeq) || 0, 0);
    return logs.read(serviceId(id), { limit, sinceSeq });
  });

  handle('logs:clear', (id) => {
    logs.clear(serviceId(id));
    return true;
  });

  handle('logs:reveal', (id) => {
    const file = logs.logFile(serviceId(id));
    if (file) shell.showItemInFolder(file);
    else shell.openPath(paths.managerLogDir());
    return file;
  });

  // config-store validates every path against the schema allowlist.
  handle('config:describe', () => config.describe());
  // Dry run for the settings screen, so it can gate Save before the write is
  // attempted. apply() re-checks; this is convenience, not the enforcement.
  handle('config:validate', (changes) => config.validate(changes));
  handle('config:apply', (changes) => {
    const result = config.apply(changes);
    // A feature toggle can add or drop setup steps (grading's environment and
    // model); check the new ones now rather than leave them at "Not checked".
    setup.checkNewlyEnabled().catch(() => { });
    return result;
  });
  handle('config:reveal', () => {
    shell.showItemInFolder(paths.configFile());
    return paths.configFile();
  });

  // Setup steps are named by id only; the runner owns every command line.
  handle('setup:list', () => {
    // config.yaml is hand-editable too, so a feature may have been switched on
    // behind the app's back. Results arrive through setup:changed.
    setup.checkNewlyEnabled().catch(() => { });
    return { sections: setup.sections(), steps: setup.snapshot() };
  });
  handle('setup:check', () => setup.checkAll());
  handle('setup:run', async (stepId, actionId) => {
    // Validates both ids and throws before anything runs.
    const { step, action } = setup.find(stepId, actionId);
    if (action.destructive) {
      const window = getWindow();
      // The action says which folder it deletes.
      const folder = action.deletes?.();
      const text = CONFIRM_TEXT[getLanguage()] ?? CONFIRM_TEXT.en;
      const options = {
        type: 'warning',
        title: 'Smart Classroom',
        message: text.message(action, step),
        detail: folder ? text.deletesFolder(folder) : text.deletesAll,
        buttons: [text.cancel, text.confirm(action)],
        defaultId: 0,
        cancelId: 0,
      };
      const { response } = window ? await dialog.showMessageBox(window, options) : await dialog.showMessageBox(options);
      if (response !== 1) return { cancelled: true };
    }
    return setup.runStep(stepId, actionId);
  });
  handle('setup:cancel', () => setup.cancel());
  handle('setup:logs', (options) => {
    const limit = Math.min(Math.max(Number(options?.limit) || 1000, 1), 5000);
    return logs.read(setupLogId, { limit });
  });
  handle('setup:clearLogs', () => {
    logs.clear(setupLogId);
    return true;
  });
  handle('setup:revealLogs', () => {
    // The sink only holds a file while a step is running; fall back to the folder.
    const file = logs.logFile(setupLogId);
    if (file) shell.showItemInFolder(file);
    else shell.openPath(paths.managerLogDir());
    return file;
  });

  // Diagnostics for a bug report. Renderer-composed, but copied here.
  handle('app:copyText', (text) => {
    if (typeof text !== 'string') throw new Error('Nothing to copy.');
    clipboard.writeText(text.slice(0, 100000));
    return true;
  });

  const send = (channel, payload) => {
    const window = getWindow();
    if (window && !window.isDestroyed()) window.webContents.send(channel, payload);
  };

  manager.on('changed', (snapshot) => send('services:changed', snapshot));
  setup.on('changed', (snapshot) => send('setup:changed', snapshot));
  logs.on('append', (batch) => send('logs:append', batch));
}

module.exports = { register };

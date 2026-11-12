// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Single source of truth for every filesystem location the Electron service
// manager touches. Nothing else in electron/services/ may build paths by hand.

const fs = require('fs');
const path = require('path');

// ui/electron/services -> ui/electron -> ui -> smart-classroom/Windows
const BUNDLED_HOME = path.resolve(__dirname, '..', '..', '..');

function isSmartClassroomHome(dir) {
  return !!dir && fs.existsSync(path.join(dir, 'main.py')) && fs.existsSync(path.join(dir, 'config.yaml'));
}

function home() {
  const override = process.env.SMART_CLASSROOM_HOME;
  if (isSmartClassroomHome(override)) return path.resolve(override);
  return packagedHome() ?? BUNDLED_HOME;
}

function packagedHome() {
  if (typeof process.resourcesPath !== 'string') return null;
  const candidate = path.join(process.resourcesPath, 'backend');
  return isSmartClassroomHome(candidate) ? candidate : null;
}

const isWindows = process.platform === 'win32';

function userDataDir() {
  if (!packagedHome()) return null;
  const override = process.env.SMART_CLASSROOM_USERDATA;
  if (override) return path.resolve(override);
  return process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, 'SmartClassroom')
    : packagedHome();
}

function venvDir() {
  const base = userDataDir() ?? path.dirname(home());
  return path.join(base, 'smartclassroom');
}

function venvPython(dir = venvDir()) {
  return isWindows ? path.join(dir, 'Scripts', 'python.exe') : path.join(dir, 'bin', 'python');
}

function layoutServiceDir() {
  return path.join(home(), 'components', 'grading', 'providers', 'layout_detection_service');
}

function configFile() {
  const data = userDataDir();
  return data ? path.join(data, 'config.yaml') : path.join(home(), 'config.yaml');
}

let ensured = false;
function ensureUserData() {
  if (ensured) return true;
  const data = userDataDir();
  if (!data) return true;
  try {
    fs.mkdirSync(data, { recursive: true });
    if (typeof process.env.SMART_CLASSROOM_USERDATA !== 'string') {
      process.env.SMART_CLASSROOM_USERDATA = data;
    }
    if (typeof process.env.PADDLE_PDX_CACHE_HOME !== 'string') {
      process.env.PADDLE_PDX_CACHE_HOME = path.join(data, 'paddlex');
    }
    if (typeof process.env.PIP_CACHE_DIR !== 'string') {
      process.env.PIP_CACHE_DIR = path.join(data, 'pip-cache');
    }
    if (typeof process.env.HF_HOME !== 'string') {
      process.env.HF_HOME = path.join(data, 'huggingface');
    }
    const target = path.join(data, 'config.yaml');
    if (!fs.existsSync(target)) {
      const src = path.join(home(), 'config.yaml');
      if (fs.existsSync(src)) fs.copyFileSync(src, target);
    }
    ensured = true;
    return true;
  } catch (e) {
    console.error('[paths] failed to initialise user data dir:', e);
    return false;
  }
}

const paths = {
  isWindows,
  home,
  userDataDir,
  ensureUserData,
  venvDir,
  venvPython,
  layoutServiceDir,
  configFile,
  bundledConfigFile: () => path.join(home(), 'config.yaml'),
  gradingConfigFile: () => {
    const data = userDataDir();
    return data ? path.join(data, 'grading', 'config.yaml') : null;
  },
  bundledGradingConfigFile: () => path.join(home(), 'components', 'grading', 'config.yaml'),
  shippedConfigStampFile: (name) => {
    const data = userDataDir();
    return data ? path.join(data, 'state', `${name}.sha256`) : null;
  },
  proxyConfigFile: () => {
    const d = userDataDir();
    return d ? path.join(d, '.proxy-config') : path.join(home(), '.proxy-config');
  },
  requirementsFile: () => path.join(home(), 'requirements.txt'),
  dlStreamerScript: () => path.join(home(), 'Scripts', 'check_dlstreamer.ps1'),
  convertVenvDir: () => (userDataDir()
    ? path.join(userDataDir(), 'venv_convert')
    : path.join(layoutServiceDir(), 'venv_convert')),
  convertRequirementsFile: () => path.join(layoutServiceDir(), 'requirements_convert.txt'),
  layoutIrModel: () => (userDataDir()
    ? path.join(userDataDir(), 'models', 'detection_model', 'PP-DocLayoutV2-ov', 'fp16', 'model.xml')
    : path.join(home(), 'models', 'detection_model', 'PP-DocLayoutV2-ov', 'fp16', 'model.xml')),
  // Logs captured by the manager itself; distinct from the app's own
  // monitoring/logs and monitoring/executionlogs trees.
  managerLogDir: () => (userDataDir()
    ? path.join(userDataDir(), 'logs', 'manager')
    : path.join(home(), 'logs', 'manager')),
  configBackupDir: () => (userDataDir()
    ? path.join(userDataDir(), 'logs', 'config-backups')
    : path.join(home(), 'logs', 'config-backups')),
};

module.exports = paths;
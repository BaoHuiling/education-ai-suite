// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Setup tasks that need administrator rights, all behind one UAC prompt.
//
// Windows asks for approval each time an elevated process starts, and setup
// has more than one task that needs one: enabling long paths and installing
// DL Streamer. So they go to one elevated helper, Scripts/run_admin_tasks.ps1,
// as a plan file, and Windows asks once.
//
// The app runs the helper unelevated with -Launch -RunAs: it starts itself
// again, elevated, and waits, so its exit code tells a declined prompt from a
// failure. An elevated process cannot pipe its output back to the unelevated
// app, so the elevated run writes a log next to the plan, which this module
// follows, and a result file it reads at the end. The app cannot stop an
// elevated process either: Cancel leaves a "stop" file that skips the tasks
// not yet started. See docs/dev-guide/first-run-wizard (D3).
//
// The launcher is the shipped script rather than code generated here and
// passed with -EncodedCommand, which security software tends to flag.

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { StringDecoder } = require('string_decoder');

const EVENT_PREFIX = '@@admin ';
const LOG_NAME = 'admin-tasks.log';
const RESULT_NAME = 'result.json';
const STOP_NAME = 'stop';

/** Exit codes of the helper's -Launch run: the elevated run's own (0, 1), or one of the rest. */
const ADMIN_EXIT = { OK: 0, FAILED: 1, LAUNCH_FAILED: 2, DECLINED: 1223 };

const DECLINED_MESSAGE = 'Administrator approval was declined.';

// Batch folders a crash left behind. Their plan can hold proxy credentials.
const STALE_MS = 24 * 60 * 60 * 1000;

const systemPowerShell = () =>
  path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');

/**
 * The arguments that start the helper's -Launch run. Without `elevate` the
 * run it starts is not elevated, for tests: no prompt.
 */
const helperArgs = ({ helper, plan, dryRun = false, elevate = true }) => [
  '-NoProfile',
  '-NonInteractive',
  '-ExecutionPolicy',
  'Bypass',
  '-File',
  helper,
  '-Plan',
  plan,
  '-Launch',
  ...(elevate ? ['-RunAs'] : []),
  ...(dryRun ? ['-DryRun'] : []),
];

/**
 * Starts the helper's -Launch run and waits for it. Resolves with its exit
 * code and what it wrote to stderr; never rejects.
 */
function launchHelper(options) {
  return new Promise((resolve) => {
    let stderr = '';
    let child;
    try {
      child = spawn(systemPowerShell(), helperArgs(options), {
        shell: false,
        windowsHide: true,
        stdio: ['ignore', 'ignore', 'pipe'],
      });
    } catch (error) {
      resolve({ code: ADMIN_EXIT.LAUNCH_FAILED, stderr: error.message });
      return;
    }
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString('utf8');
    });
    child.on('error', (error) => resolve({ code: ADMIN_EXIT.LAUNCH_FAILED, stderr: error.message }));
    child.on('close', (code) => resolve({ code: code ?? ADMIN_EXIT.FAILED, stderr: stderr.trim() }));
  });
}

/** The event on a log line, or null for a line of output. */
function parseLine(line) {
  if (!line.startsWith(EVENT_PREFIX)) return null;
  try {
    const event = JSON.parse(line.slice(EVENT_PREFIX.length));
    return event && typeof event === 'object' && typeof event.event === 'string' ? event : null;
  } catch {
    return null;
  }
}

/**
 * Calls onLine for each line appended to a file, which need not exist yet.
 * Polls, and opens the file only to read, so the writer never finds it
 * locked. stop() reads what is left, including a last line with no newline.
 */
function followFile(file, onLine, { interval = 250 } = {}) {
  const decoder = new StringDecoder('utf8');
  const buffer = Buffer.alloc(64 * 1024);
  let offset = 0;
  let tail = '';
  let reading = null;

  const take = (text) => {
    const lines = (tail + text).split('\n');
    tail = lines.pop();
    for (const line of lines) onLine(line.replace(/\r$/, ''));
  };

  const read = async () => {
    let handle;
    try {
      handle = await fs.promises.open(file, 'r');
    } catch {
      return; // Not written yet.
    }
    try {
      for (;;) {
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
        if (!bytesRead) break;
        offset += bytesRead;
        take(decoder.write(buffer.subarray(0, bytesRead)));
      }
    } finally {
      await handle.close();
    }
  };

  const poll = () => {
    if (!reading) {
      reading = read()
        .catch(() => undefined)
        .finally(() => {
          reading = null;
        });
    }
    return reading;
  };

  const timer = setInterval(poll, interval);
  return {
    async stop() {
      clearInterval(timer);
      await reading;
      await poll();
      take(decoder.end());
      if (tail) onLine(tail.replace(/\r$/, ''));
      tail = '';
    },
  };
}

function readResult(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return null;
  }
}

function sweepStale(baseDir, now = Date.now()) {
  let names = [];
  try {
    names = fs.readdirSync(baseDir);
  } catch {
    return;
  }
  for (const name of names) {
    if (!name.startsWith('run-')) continue;
    const dir = path.join(baseDir, name);
    try {
      if (now - fs.statSync(dir).mtimeMs > STALE_MS) fs.rmSync(dir, { recursive: true, force: true });
    } catch {
      // In use or already gone; the next run tries again.
    }
  }
}

/** What a batch came to, from the launcher's exit code and the helper's result file. */
function outcomeOf(launched, report) {
  if (launched.code === ADMIN_EXIT.DECLINED) return { declined: true, tasks: {}, error: DECLINED_MESSAGE };
  if (!report || typeof report.tasks !== 'object' || !report.tasks) {
    const error =
      launched.code === ADMIN_EXIT.LAUNCH_FAILED
        ? `Windows could not start the administrator helper${launched.stderr ? `: ${launched.stderr}` : '.'}`
        : `The administrator helper stopped without a result (exit code ${launched.code}).`;
    return { declined: false, tasks: {}, error };
  }
  const { _helper: helper, ...tasks } = report.tasks;
  return { declined: false, tasks, error: helper?.error ?? null };
}

/**
 * Run admin tasks in one elevated helper. Throws when the helper is missing;
 * otherwise returns at once with:
 *   done  resolves with { declined, tasks: { <id>: { ok, error, exitCode,
 *         skipped } }, error }; a task missing from `tasks` never ran, and
 *         `error` says why. Never rejects for anything the helper does.
 *   stop  skips the tasks not yet started. One under way runs to its end.
 *
 * onEvent gets the helper's events as they happen ({event:'task', task,
 * state, message}, ...), onOutput every other line it logs. The batch folder
 * is removed afterwards: its plan can hold proxy credentials.
 */
function runAdminTasks({
  tasks,
  helper,
  baseDir,
  dryRun = false,
  elevate = true,
  onEvent = () => undefined,
  onOutput = () => undefined,
  launch = launchHelper,
  pollInterval = 250,
}) {
  if (!fs.existsSync(helper)) throw new Error(`${helper} is missing.`);
  fs.mkdirSync(baseDir, { recursive: true });
  sweepStale(baseDir);
  const dir = fs.mkdtempSync(path.join(baseDir, 'run-'));

  const done = (async () => {
    const plan = path.join(dir, 'plan.json');
    fs.writeFileSync(plan, JSON.stringify({ tasks }), 'utf8');
    const follower = followFile(
      path.join(dir, LOG_NAME),
      (line) => {
        const event = parseLine(line);
        if (event) onEvent(event);
        else if (line.trim()) onOutput(line);
      },
      { interval: pollInterval }
    );
    let launched;
    try {
      // The folder too, for a stand-in in tests.
      launched = await launch({ helper, plan, dryRun, elevate, dir });
    } finally {
      await follower.stop();
    }
    return outcomeOf(launched, readResult(path.join(dir, RESULT_NAME)));
  })().finally(() => {
    try {
      // Retries: an antivirus scan can hold a file just written.
      fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3 });
    } catch {
      // Swept by a later batch (sweepStale); the outcome stands.
    }
  });

  return {
    dir,
    done,
    stop() {
      try {
        fs.writeFileSync(path.join(dir, STOP_NAME), '');
      } catch {
        // The batch has finished and its folder is gone.
      }
    },
  };
}

module.exports = {
  ADMIN_EXIT,
  DECLINED_MESSAGE,
  EVENT_PREFIX,
  followFile,
  helperArgs,
  launchHelper,
  outcomeOf,
  parseLine,
  runAdminTasks,
  sweepStale,
};

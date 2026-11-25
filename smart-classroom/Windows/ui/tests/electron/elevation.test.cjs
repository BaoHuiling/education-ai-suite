// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The administrator tasks' plumbing: the launcher script, the log the
// elevated helper writes and the result it leaves. Nothing here elevates: the
// helper runs as a dry run, started without "runas", so no UAC prompt appears.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const elevation = require('../../electron/services/elevation.cjs');

const HELPER = path.resolve(__dirname, '..', '..', '..', 'Scripts', 'run_admin_tasks.ps1');

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sc-elevation-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

const event = (fields) => `${elevation.EVENT_PREFIX}${JSON.stringify(fields)}`;

test('the app asks for elevation unless told not to, and passes a dry run on', () => {
  const helper = String.raw`C:\Program Files\Smart Classroom\resources\backend\Scripts\run_admin_tasks.ps1`;
  const plan = String.raw`C:\Users\O'Brien\AppData\Local\SmartClassroom\state\admin-tasks\run-x\plan.json`;
  const app = elevation.helperArgs({ helper, plan });
  // Arguments, not a command line: spawn quotes them, so a path with spaces stays whole.
  assert.deepEqual(app.slice(4), ['-File', helper, '-Plan', plan, '-Launch', '-RunAs']);
  assert.ok(!app.includes('-EncodedCommand'));

  const plain = elevation.helperArgs({ helper: 'h.ps1', plan: 'p.json', elevate: false, dryRun: true });
  assert.ok(!plain.includes('-RunAs'));
  assert.ok(plain.includes('-DryRun'));
});

test('parseLine reads events and leaves everything else as output', () => {
  assert.deepEqual(elevation.parseLine(event({ event: 'task', task: 'longPaths', state: 'ok' })), {
    event: 'task',
    task: 'longPaths',
    state: 'ok',
  });
  assert.equal(elevation.parseLine('Downloading DL Streamer...'), null);
  assert.equal(elevation.parseLine(`${elevation.EVENT_PREFIX}{not json`), null);
  assert.equal(elevation.parseLine(`${elevation.EVENT_PREFIX}{"task":"x"}`), null);
  // ConvertTo-Json in Windows PowerShell escapes quotes and angle brackets.
  assert.equal(elevation.parseLine(`${elevation.EVENT_PREFIX}{"event":"task","message":"can\\u0027t \\u003cx\\u003e"}`).message, "can't <x>");
});

test('followFile reads a file as it grows, a character split across writes included', async (t) => {
  const file = path.join(tempDir(t), 'admin-tasks.log');
  const lines = [];
  const follower = elevation.followFile(file, (line) => lines.push(line), { interval: 10 });

  // The file does not exist yet: nothing happens.
  await new Promise((resolve) => setTimeout(resolve, 30));
  const text = Buffer.from('first\r\n中文 line\nlast, no newline', 'utf8');
  const split = text.indexOf(Buffer.from('中', 'utf8')) + 1;
  fs.writeFileSync(file, text.subarray(0, split));
  await new Promise((resolve) => setTimeout(resolve, 40));
  fs.appendFileSync(file, text.subarray(split));
  await follower.stop();

  assert.deepEqual(lines, ['first', '中文 line', 'last, no newline']);
});

test('outcomeOf tells a declined prompt, a failed launch and a missing result apart', () => {
  const { ADMIN_EXIT, DECLINED_MESSAGE, outcomeOf } = elevation;
  assert.deepEqual(outcomeOf({ code: ADMIN_EXIT.DECLINED, stderr: '' }, null), {
    declined: true,
    tasks: {},
    error: DECLINED_MESSAGE,
  });
  assert.match(outcomeOf({ code: ADMIN_EXIT.LAUNCH_FAILED, stderr: 'No such file' }, null).error, /could not start.*No such file/);
  assert.match(outcomeOf({ code: 1, stderr: '' }, null).error, /without a result \(exit code 1\)/);

  const report = { tasks: { longPaths: { ok: true }, _helper: { ok: false, error: 'Plan unreadable' } } };
  assert.deepEqual(outcomeOf({ code: 1, stderr: '' }, report), {
    declined: false,
    tasks: { longPaths: { ok: true } },
    error: 'Plan unreadable',
  });
});

test('runAdminTasks hands over a plan, relays the log and cleans up after itself', async (t) => {
  const baseDir = tempDir(t);
  const events = [];
  const output = [];
  let planSeen = null;

  const batch = elevation.runAdminTasks({
    tasks: [{ id: 'longPaths' }, { id: 'dlStreamer', requiredVersion: '2026.1.0', httpsProxy: 'http://proxy:911' }],
    helper: HELPER,
    baseDir,
    pollInterval: 10,
    onEvent: (entry) => events.push(entry),
    onOutput: (line) => output.push(line),
    // Stands in for the elevated helper: writes what it would.
    async launch({ dir, plan }) {
      planSeen = JSON.parse(fs.readFileSync(plan, 'utf8'));
      const log = path.join(dir, 'admin-tasks.log');
      fs.appendFileSync(log, `${event({ event: 'started' })}\n`);
      fs.appendFileSync(log, `${event({ event: 'task', task: 'longPaths', state: 'running' })}\nWindows long paths are enabled.\n`);
      fs.appendFileSync(log, `${event({ event: 'task', task: 'longPaths', state: 'ok' })}\n`);
      fs.writeFileSync(
        path.join(dir, 'result.json'),
        '\uFEFF' + JSON.stringify({ dryRun: false, tasks: { longPaths: { ok: true }, dlStreamer: { ok: false, exitCode: 2, error: 'no' } } })
      );
      return { code: 1, stderr: '' };
    },
  });
  const outcome = await batch.done;

  assert.deepEqual(planSeen.tasks.map((task) => task.id), ['longPaths', 'dlStreamer']);
  assert.equal(planSeen.tasks[1].httpsProxy, 'http://proxy:911');
  assert.deepEqual(events.map((entry) => entry.state ?? entry.event), ['started', 'running', 'ok']);
  assert.deepEqual(output, ['Windows long paths are enabled.']);
  assert.deepEqual(outcome.tasks.dlStreamer, { ok: false, exitCode: 2, error: 'no' });
  assert.equal(outcome.declined, false);
  // The plan can hold proxy credentials, so the folder goes.
  assert.equal(fs.existsSync(batch.dir), false);
});

test('stop() leaves the marker the helper looks for', async (t) => {
  let release;
  let stopSeen = false;
  const batch = elevation.runAdminTasks({
    tasks: [{ id: 'longPaths' }],
    helper: HELPER,
    baseDir: tempDir(t),
    pollInterval: 10,
    launch: ({ dir }) =>
      new Promise((resolve) => {
        release = () => {
          stopSeen = fs.existsSync(path.join(dir, 'stop'));
          resolve({ code: 1, stderr: '' });
        };
      }),
  });
  batch.stop();
  release();
  await batch.done;
  assert.equal(stopSeen, true);
});

test('a missing helper fails before anything is written', (t) => {
  const baseDir = path.join(tempDir(t), 'never');
  assert.throws(
    () => elevation.runAdminTasks({ tasks: [], helper: path.join(baseDir, 'gone.ps1'), baseDir }),
    /gone\.ps1 is missing/
  );
  assert.equal(fs.existsSync(baseDir), false);
});

test('folders a crash left behind are swept after a day, others kept', (t) => {
  const baseDir = tempDir(t);
  const old = path.join(baseDir, 'run-old');
  const fresh = path.join(baseDir, 'run-fresh');
  const other = path.join(baseDir, 'keep');
  for (const dir of [old, fresh, other]) fs.mkdirSync(dir);
  const dayAgo = new Date(Date.now() - 25 * 60 * 60 * 1000);
  fs.utimesSync(old, dayAgo, dayAgo);
  fs.utimesSync(other, dayAgo, dayAgo);

  elevation.sweepStale(baseDir);

  assert.equal(fs.existsSync(old), false);
  assert.equal(fs.existsSync(fresh), true);
  assert.equal(fs.existsSync(other), true);
});

// The real helper and launcher, as a dry run and without "runas": everything
// but the UAC prompt itself.
const onWindows = { skip: process.platform !== 'win32' && 'needs Windows PowerShell' };

function dryRun(t, tasks, { stop = false } = {}) {
  const events = [];
  const output = [];
  const batch = elevation.runAdminTasks({
    tasks,
    helper: HELPER,
    baseDir: tempDir(t),
    dryRun: true,
    elevate: false,
    pollInterval: 50,
    onEvent: (entry) => events.push(entry),
    onOutput: (line) => output.push(line),
  });
  if (stop) batch.stop();
  return batch.done.then((outcome) => ({ outcome, events, output }));
}

test('the helper runs a plan through, as a dry run', onWindows, async (t) => {
  const { outcome, events, output } = await dryRun(t, [
    { id: 'longPaths' },
    { id: 'dlStreamer', requiredVersion: '2026.1.0', httpProxy: 'http://user:pw@proxy.example.com:911' },
  ]);
  assert.equal(outcome.error, null);
  assert.deepEqual(outcome.tasks, { longPaths: { ok: true }, dlStreamer: { ok: true } });
  assert.deepEqual(
    events.map((entry) => (entry.event === 'task' ? `${entry.task}:${entry.state}` : entry.event)),
    ['started', 'longPaths:running', 'longPaths:ok', 'dlStreamer:running', 'dlStreamer:ok', 'done']
  );
  assert.ok(output.includes('Dry run: would install DL Streamer 2026.1.0.'));
  // Says whether Windows elevated it: what the elevated dry run checks.
  assert.equal(typeof events[0].admin, 'boolean');
});

test('the launch keeps paths with spaces and quotes whole', onWindows, async (t) => {
  const root = path.join(tempDir(t), "it's a folder");
  fs.mkdirSync(path.join(root, 'Scripts'), { recursive: true });
  const helper = path.join(root, 'Scripts', 'run_admin_tasks.ps1');
  fs.copyFileSync(HELPER, helper);

  const outcome = await elevation.runAdminTasks({
    tasks: [{ id: 'longPaths' }],
    helper,
    baseDir: path.join(root, 'state', 'admin tasks'),
    dryRun: true,
    elevate: false,
    pollInterval: 50,
  }).done;

  assert.equal(outcome.error, null);
  assert.deepEqual(outcome.tasks, { longPaths: { ok: true } });
});

test('the helper refuses what it cannot pass on safely', onWindows, async (t) => {
  const { outcome } = await dryRun(t, [
    { id: 'dlStreamer', requiredVersion: '2026.1.0; Remove-Item C:\\' },
    { id: 'somethingElse' },
  ]);
  assert.match(outcome.tasks.dlStreamer.error, /Not a DL Streamer version/);
  assert.match(outcome.tasks.somethingElse.error, /Unknown task 'somethingElse'/);

  const proxy = await dryRun(t, [{ id: 'dlStreamer', requiredVersion: '2026.1.0', httpsProxy: 'http://proxy" -Other x' }]);
  assert.match(proxy.outcome.tasks.dlStreamer.error, /HttpsProxy setting is not a proxy URL/);
});

test('the helper skips every task once stop is asked for', onWindows, async (t) => {
  const { outcome, events } = await dryRun(t, [{ id: 'longPaths' }, { id: 'dlStreamer', requiredVersion: '2026.1.0' }], {
    stop: true,
  });
  assert.equal(outcome.tasks.longPaths.skipped, true);
  assert.equal(outcome.tasks.dlStreamer.skipped, true);
  assert.ok(!events.some((entry) => entry.state === 'running'));
});

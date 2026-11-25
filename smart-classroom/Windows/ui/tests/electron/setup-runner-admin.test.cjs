// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The setup steps that need administrator rights run in one elevated helper,
// so Windows asks once. A stand-in replaces the helper here, and the two
// steps' checks are stubbed, so nothing is changed and no UAC prompt appears.

const test = require('node:test');
const assert = require('node:assert/strict');

const { SetupRunner, SETUP_ERROR } = require('../../electron/services/setup-runner.cjs');
const registry = require('../../electron/services/registry.cjs');
const { DECLINED_MESSAGE } = require('../../electron/services/elevation.cjs');

// DL Streamer is asked for only with video analytics on.
const realEffectivelyEnabled = registry.effectivelyEnabled;
registry.effectivelyEnabled = (id) => (id === 'video_analytics' ? true : realEffectivelyEnabled(id));

const ADMIN_STEPS = { longPaths: 'enable', dlStreamer: 'install' };

// The steps are module-level objects, so their real checks are put back after
// each test, whatever order the stubs went on in.
const REAL_CHECKS = new Map();

/**
 * A stand-in for elevation.runAdminTasks. `play` acts the helper's part: it
 * gets the batch's options and returns what the batch came to.
 */
function fakeHelper(play) {
  const launches = [];
  const adminTasks = (options) => {
    const batch = { stopped: false };
    launches.push(options.tasks);
    batch.done = Promise.resolve().then(() => play(options, batch));
    batch.stop = () => {
      batch.stopped = true;
    };
    return batch;
  };
  return { adminTasks, launches };
}

const task = (id, state, message) => ({ event: 'task', task: id, state, ...(message ? { message } : {}) });
const done = (tasks) => ({ declined: false, tasks, error: null });

/**
 * A runner where only the administrator steps need work. Their checks report
 * `after` once a task has run (missing before), and the auto steps are stubbed.
 */
function runner(t, adminTasks, { after = { longPaths: 'ok', dlStreamer: 'ok' }, missing = [] } = {}) {
  const lines = [];
  const logs = { note: (id, line) => lines.push(line), openSink() { }, closeSink() { } };
  const setup = new SetupRunner(logs, { adminTasks });
  for (const id of setup.state.keys()) {
    setup.state.set(id, { ...setup.state.get(id), status: 'ok', repair: null });
  }
  for (const [id, actionId] of Object.entries(ADMIN_STEPS)) {
    setup.state.set(id, { ...setup.state.get(id), status: 'missing', repair: actionId });
  }
  for (const id of missing) {
    setup.state.set(id, { ...setup.state.get(id), status: 'missing', repair: 'install' });
  }

  const ran = new Set();
  const checks = [];
  for (const id of Object.keys(ADMIN_STEPS)) {
    const { step } = setup.find(id, ADMIN_STEPS[id]);
    if (!REAL_CHECKS.has(id)) REAL_CHECKS.set(id, step.check);
    step.check = async () => {
      checks.push(id);
      return ran.has(id) ? { status: after[id], detail: `${id} ${after[id]}` } : { status: 'missing', detail: `${id} missing` };
    };
    t.after(() => {
      step.check = REAL_CHECKS.get(id);
    });
  }
  setup.refreshEnvironment = async () => undefined;
  setup.checkBackendStopped = async () => undefined;

  const autoCalls = [];
  setup.runStep = ((original) => async (stepId, actionId) => {
    if (stepId in ADMIN_STEPS) return original.call(setup, stepId, actionId);
    autoCalls.push(stepId);
    return setup.state.get(stepId);
  })(setup.runStep);

  return { setup, lines, ran, checks, autoCalls };
}

test('both administrator tasks go to one helper, and each step is checked as its own task ends', async (t) => {
  let harness;
  const timeline = [];
  const { adminTasks, launches } = fakeHelper(async ({ onEvent }) => {
    for (const id of ['longPaths', 'dlStreamer']) {
      onEvent(task(id, 'running'));
      harness.ran.add(id);
      onEvent(task(id, 'ok'));
    }
    return done({ longPaths: { ok: true }, dlStreamer: { ok: true } });
  });
  harness = runner(t, adminTasks);
  const { setup } = harness;
  setup.on('changed', () => {
    const state = `${setup.state.get('longPaths').status}/${setup.state.get('dlStreamer').status}`;
    if (timeline[timeline.length - 1] !== state) timeline.push(state);
  });

  const result = await setup.runAutomatic({ includeElevated: true });

  assert.equal(launches.length, 1);
  assert.deepEqual(launches[0].map((entry) => entry.id), ['longPaths', 'dlStreamer']);
  assert.match(launches[0][1].requiredVersion, /^\d+\.\d+\.\d+$/);
  assert.deepEqual(result.ran, ['longPaths', 'dlStreamer']);
  assert.deepEqual(result.failed, []);
  // Long paths reads as fixed while DL Streamer is still installing.
  assert.ok(timeline.includes('ok/running'), timeline.join(' → '));
  assert.equal(setup.state.get('dlStreamer').status, 'ok');
});

test("the log keeps the helper's order: each task's heading, then its output", async (t) => {
  let harness;
  const { adminTasks } = fakeHelper(async ({ onEvent, onOutput }) => {
    // As the follower delivers one read of the log: all at once.
    onEvent(task('longPaths', 'running'));
    onOutput('Windows long paths are enabled.');
    harness.ran.add('longPaths');
    onEvent(task('longPaths', 'ok'));
    onEvent(task('dlStreamer', 'running'));
    onOutput('Step 1: Download Installer');
    harness.ran.add('dlStreamer');
    onEvent(task('dlStreamer', 'ok'));
    return done({ longPaths: { ok: true }, dlStreamer: { ok: true } });
  });
  harness = runner(t, adminTasks);

  await harness.setup.runAutomatic({ includeElevated: true });

  const lines = harness.lines.filter((line) => !/^=== Administrator tasks|^Asking Windows/.test(line));
  assert.deepEqual(lines, [
    '=== Windows long paths: Enable ===',
    'Windows long paths are enabled.',
    '=== Windows long paths: ok ===',
    '=== DL Streamer: Install ===',
    'Step 1: Download Installer',
    '=== DL Streamer: ok ===',
  ]);
});

test('a declined prompt fails every task in the batch, and says why', async (t) => {
  const { adminTasks } = fakeHelper(async () => ({ declined: true, tasks: {}, error: DECLINED_MESSAGE }));
  const { setup } = runner(t, adminTasks);

  const result = await setup.runAutomatic({ includeElevated: true });

  assert.deepEqual(result.failed, [
    { id: 'longPaths', error: DECLINED_MESSAGE },
    { id: 'dlStreamer', error: DECLINED_MESSAGE },
  ]);
  for (const [id, actionId] of Object.entries(ADMIN_STEPS)) {
    const state = setup.state.get(id);
    assert.equal(state.status, 'failed');
    assert.equal(state.detail, DECLINED_MESSAGE);
    // Still offered, for another try.
    assert.equal(state.repair, actionId);
  }
});

test('check_dlstreamer.ps1 exit codes still say what to do', async (t) => {
  for (const [exitCode, expected] of [
    [1, /The upgrade to DL Streamer \d+\.\d+\.\d+ failed; install it from https:\/\/github\.com\//],
    [2, /DL Streamer was not installed; install it from https:\/\/github\.com\//],
    [5, /exited with code 5/],
  ]) {
    const { adminTasks } = fakeHelper(async () =>
      done({ longPaths: { ok: true }, dlStreamer: { ok: false, exitCode, error: `check_dlstreamer.ps1 exited with code ${exitCode}.` } })
    );
    const { setup, ran } = runner(t, adminTasks);
    ran.add('longPaths');
    const result = await setup.runAutomatic({ includeElevated: true });
    assert.deepEqual(result.ran, ['longPaths']);
    assert.equal(result.failed.length, 1);
    assert.match(result.failed[0].error, expected);
    assert.match(setup.state.get('dlStreamer').detail, expected);
  }
});

test("a row's own button runs its task through the helper too", async (t) => {
  let harness;
  const { adminTasks, launches } = fakeHelper(async ({ onEvent }) => {
    onEvent(task('longPaths', 'running'));
    harness.ran.add('longPaths');
    return done({ longPaths: { ok: true } });
  });
  harness = runner(t, adminTasks);

  const state = await harness.setup.runStep('longPaths', 'enable');

  assert.deepEqual(launches, [[{ id: 'longPaths' }]]);
  assert.equal(state.status, 'ok');
  assert.equal(harness.setup.busy, false);
});

test("a row's own button rejects with the reason when the prompt is declined", async (t) => {
  const { adminTasks } = fakeHelper(async () => ({ declined: true, tasks: {}, error: DECLINED_MESSAGE }));
  const { setup } = runner(t, adminTasks);
  await assert.rejects(setup.runStep('dlStreamer', 'install'), { message: DECLINED_MESSAGE });
  assert.equal(setup.state.get('dlStreamer').status, 'failed');
});

test('with the backend running, DL Streamer stays out of the batch and long paths does not', async (t) => {
  let harness;
  const { adminTasks, launches } = fakeHelper(async () => {
    harness.ran.add('longPaths');
    return done({ longPaths: { ok: true } });
  });
  harness = runner(t, adminTasks);
  const { setup } = harness;
  setup.checkBackendStopped = async (action) => {
    if (action.requiresBackendStopped) {
      throw Object.assign(new Error('Stop the backend first: it has files this step replaces in use.'), {
        code: SETUP_ERROR.BACKEND_RUNNING,
      });
    }
  };

  const result = await setup.runAutomatic({ includeElevated: true });

  assert.deepEqual(launches.map((tasks) => tasks.map((entry) => entry.id)), [['longPaths']]);
  assert.deepEqual(result.ran, ['longPaths']);
  assert.match(result.failed[0].error, /Stop the backend first/);
  // Refused before anything ran, as runStep refuses: the row keeps its state.
  assert.equal(setup.state.get('dlStreamer').status, 'missing');

  // And alone, from its own button, Windows is not asked at all.
  await assert.rejects(setup.runStep('dlStreamer', 'install'), { code: SETUP_ERROR.BACKEND_RUNNING });
  assert.equal(launches.length, 1);
});

test('Cancel skips the tasks not yet started and ends the run after the one under way', async (t) => {
  let harness;
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const { adminTasks } = fakeHelper(async ({ onEvent }, batch) => {
    onEvent(task('longPaths', 'running'));
    await gate;
    harness.ran.add('longPaths');
    onEvent(task('longPaths', 'ok'));
    return done({
      longPaths: { ok: true },
      dlStreamer: batch.stopped ? { ok: false, skipped: true, error: 'Cancelled before it started.' } : { ok: true },
    });
  });
  harness = runner(t, adminTasks, { missing: ['python'] });
  const { setup, autoCalls, lines } = harness;

  const run = setup.runAutomatic({ includeElevated: true });
  while (!setup.adminBatch) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(setup.cancel(), true);
  release();
  const result = await run;

  assert.deepEqual(result.ran, ['longPaths']);
  // Skipped is not failed: nothing was tried.
  assert.deepEqual(result.failed, []);
  assert.equal(setup.state.get('dlStreamer').status, 'missing');
  // The rest of the run does not start.
  assert.deepEqual(autoCalls, []);
  assert.ok(lines.some((line) => /Cancellation requested/.test(line)));
});

test('a helper that cannot be started fails the batch with the reason', async (t) => {
  const adminTasks = () => {
    throw new Error('C:\\app\\Scripts\\run_admin_tasks.ps1 is missing.');
  };
  const { setup } = runner(t, adminTasks);

  const result = await setup.runAutomatic({ includeElevated: true });

  assert.deepEqual(
    result.failed.map((entry) => entry.id),
    ['longPaths', 'dlStreamer']
  );
  assert.match(result.failed[0].error, /run_admin_tasks\.ps1 is missing/);
  assert.equal(setup.busy, false);
  assert.equal(setup.adminBatch, null);
});

test('without approval the helper is never started', async (t) => {
  const { adminTasks, launches } = fakeHelper(async () => done({}));
  const { setup } = runner(t, adminTasks);
  const result = await setup.runAutomatic();
  assert.equal(launches.length, 0);
  assert.deepEqual(result.manual, ['longPaths', 'dlStreamer']);
});

test('every action that needs elevation runs through the administrator helper', (t) => {
  const { adminTasks } = fakeHelper(async () => done({}));
  const { setup } = runner(t, adminTasks);
  for (const id of setup.state.keys()) {
    setup.state.set(id, { ...setup.state.get(id), status: 'missing' });
  }
  const elevated = setup
    .snapshot()
    .flatMap((step) => step.actions.filter((action) => action.elevates).map((action) => setup.find(step.id, action.id).action));
  assert.ok(elevated.length >= 2);
  for (const action of elevated) assert.equal(typeof action.adminTask, 'function', action.label);
});

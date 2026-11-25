// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The first-run wizard's flow: the record, the launch decision, the flow as a
// pure function, and the controller that runs it. The controller is driven
// with a fake setup runner, a fake clock and fake timers, so nothing installs
// and nothing waits.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { EventEmitter } = require('events');

const {
  PHASE,
  COUNTDOWN_SECONDS,
  readRecord,
  writeRecord,
  launchPlan,
  initialState,
  next,
  FirstRunController,
} = require('../../electron/services/first-run.cjs');
const { HOSTS } = require('../../electron/services/network-probe.cjs');

const NOW = 1_000_000;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const results = (failingGroups = []) =>
  HOSTS.map((entry) => ({
    ...entry,
    ok: !failingGroups.includes(entry.group),
    cause: failingGroups.includes(entry.group) ? 'timeout' : null,
    detail: '',
    ms: 1,
  }));

// The Windows proxy works and is not saved yet.
const PROXY = 'http://user:secret@proxy.example.com:911';
const WORKING_NETWORK = {
  saved: null,
  chosen: { source: 'system', httpProxy: PROXY, httpsProxy: PROXY, noProxy: '', results: results() },
  tried: [{ source: 'system', httpProxy: PROXY, httpsProxy: PROXY, noProxy: '', results: results() }],
};
const FAILED_NETWORK = {
  saved: null,
  chosen: null,
  tried: [{ source: 'direct', httpProxy: '', httpsProxy: '', noProxy: '', results: results(['packages']) }],
};

class FakeRunner extends EventEmitter {
  constructor({ disk = 'ok', venvAfter = 'ok', onRun } = {}) {
    super();
    this.disk = disk;
    this.venvAfter = venvAfter;
    this.onRun = onRun;
    this.venvStatus = 'missing';
    this.busy = false;
    this.autoCancelled = false;
    this.runs = [];
  }

  async checkAll() {
    return [
      { id: 'disk', status: this.disk },
      { id: 'venv', status: this.venvStatus },
    ];
  }

  plan() {
    return [{ stepId: 'venv', actionId: 'create', kind: 'auto' }];
  }

  async runAutomatic({ includeElevated = false } = {}) {
    this.autoCancelled = false;
    this.runs.push(includeElevated);
    await this.onRun?.(this, this.runs.length);
    if (!this.autoCancelled) this.venvStatus = this.venvAfter;
    return { ran: ['venv'], failed: [], blocked: [], manual: [] };
  }

  snapshot() {
    return [{ id: 'venv', status: this.venvStatus }];
  }
}

function harness(t, { record, runner = new FakeRunner(), network = WORKING_NETWORK } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sc-first-run-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'state', 'first-run.json');
  if (record) writeRecord(file, { schema: 1, stage: 'welcome', mode: 'recommended', completedAt: null, ...record });

  const timers = [];
  const applied = [];
  const logs = [];
  let backendStarts = 0;
  const controller = new FirstRunController({
    recordFile: () => file,
    setupRunner: runner,
    testNetwork: async () => network,
    configuredHub: () => 'huggingface',
    applyChanges: (changes) => applied.push(...changes),
    startBackend: () => {
      backendStarts += 1;
    },
    log: (line) => logs.push(line),
    now: () => NOW,
    setTimer: (fn, ms) => {
      const timer = { fn, ms, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimer: (timer) => {
      timer.cleared = true;
    },
  });
  return { controller, runner, file, timers, applied, logs, starts: () => backendStarts };
}

/** Resolve with the first snapshot that satisfies `predicate`. */
function waitFor(controller, predicate) {
  return new Promise((resolve, reject) => {
    const current = controller.snapshot();
    if (predicate(current)) {
      resolve(current);
      return;
    }
    const timer = setTimeout(() => {
      controller.off('changed', listener);
      reject(new Error(`timed out waiting; last snapshot: ${JSON.stringify(controller.snapshot())}`));
    }, 2000);
    const listener = (snapshot) => {
      if (!predicate(snapshot)) return;
      clearTimeout(timer);
      controller.off('changed', listener);
      resolve(snapshot);
    };
    controller.on('changed', listener);
  });
}

const inPhase = (phase) => (snapshot) => snapshot.phase === phase;

/** Start the flow and let the countdown run out, as the main process would. */
async function startAndInstall({ controller, timers }) {
  controller.start();
  await waitFor(controller, inPhase(PHASE.COUNTDOWN));
  timers[timers.length - 1].fn();
}

// ---------------------------------------------------------------------------
// Launch decision and record
// ---------------------------------------------------------------------------

test('launchPlan() decides from the environment first, then the record', () => {
  const record = (patch) => ({ schema: 1, stage: 'welcome', mode: 'recommended', completedAt: null, ...patch });
  assert.equal(launchPlan({ record: null, envOk: true }), 'ready');
  assert.equal(launchPlan({ record: record({ mode: 'manual' }), envOk: true }), 'ready');
  assert.equal(launchPlan({ record: null, envOk: false }), 'wizard');
  assert.equal(launchPlan({ record: record({ stage: 'installing' }), envOk: false }), 'wizard');
  assert.equal(launchPlan({ record: record({ completedAt: '2026-11-18T00:00:00.000Z' }), envOk: false }), 'repair');
  assert.equal(launchPlan({ record: record({ mode: 'manual' }), envOk: false }), 'manual');
  assert.equal(
    launchPlan({ record: record({ mode: 'manual', completedAt: '2026-11-18T00:00:00.000Z' }), envOk: false }),
    'repair'
  );
});

test('a missing, corrupt or foreign record reads as none', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sc-first-run-record-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'first-run.json');

  assert.equal(readRecord(file), null);
  fs.writeFileSync(file, '{ not json');
  assert.equal(readRecord(file), null);
  fs.writeFileSync(file, JSON.stringify({ schema: 2, stage: 'welcome', mode: 'recommended', completedAt: null }));
  assert.equal(readRecord(file), null);
  fs.writeFileSync(file, JSON.stringify({ schema: 1, stage: 'elsewhere', mode: 'recommended', completedAt: null }));
  assert.equal(readRecord(file), null);
  fs.writeFileSync(file, JSON.stringify({ schema: 1, stage: 'done', mode: 'recommended', completedAt: 5 }));
  assert.equal(readRecord(file), null);
});

test('a record round-trips, creating its folder and leaving no temporary file', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sc-first-run-record-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, 'state', 'first-run.json');
  const record = { schema: 1, stage: 'customize', mode: 'custom', completedAt: null };

  writeRecord(file, record);
  assert.deepEqual(readRecord(file), record);
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ['first-run.json']);
});

// ---------------------------------------------------------------------------
// The flow as a pure function
// ---------------------------------------------------------------------------

const checked = (state, { network = { ok: true }, diskLow = false } = {}) =>
  next(state, { type: 'checked', network, diskLow }, NOW);

test('the checks lead to a countdown, which runs here whether or not a window shows it', () => {
  const state = checked(initialState());
  assert.equal(state.phase, PHASE.COUNTDOWN);
  assert.equal(state.endsAt, NOW + COUNTDOWN_SECONDS * 1000);
});

test('low disk space stops the flow until someone accepts it', () => {
  const disk = checked(initialState(), { diskLow: true });
  assert.equal(disk.phase, PHASE.WAITING);
  assert.equal(disk.held, 'disk');
  assert.equal(disk.endsAt, null);
  assert.equal(next(disk, { type: 'timeout' }, NOW), disk);
  assert.equal(checked(initialState({ autoStart: false }), { diskLow: true }).held, 'disk');
  // Installing is the user's choice, with their settings.
  const installing = next(disk, { type: 'installNow' }, NOW);
  assert.equal(installing.phase, PHASE.INSTALLING);
  assert.equal(installing.recommended, false);
  // The installation the user agreed to before the app was closed goes on.
  assert.equal(checked(initialState({ resume: true }), { diskLow: true }).phase, PHASE.INSTALLING);
});

test('a failed network test stops the flow until someone fixes the network', () => {
  for (const network of [{ ok: false }, null]) {
    const state = checked(initialState(), { network, diskLow: true });
    assert.equal(state.phase, PHASE.WAITING);
    // The network first: it is what the user can fix on the page that shows.
    assert.equal(state.held, 'network');
    assert.equal(state.endsAt, null);
    // Nothing counts down, so nothing installs by itself.
    assert.equal(next(state, { type: 'timeout' }, NOW), state);
  }
  // Even with automatic start off: the network is what needs a person.
  assert.equal(checked(initialState({ autoStart: false }), { network: { ok: false } }).held, 'network');
  // A user who already chose to customize keeps their place.
  const customizing = next(initialState(), { type: 'hold', reason: 'customize' }, NOW);
  assert.equal(checked(customizing, { network: { ok: false } }).held, 'customize');
  // The installation the user agreed to before the app was closed goes on.
  assert.equal(checked(initialState({ resume: true }), { network: { ok: false } }).phase, PHASE.INSTALLING);
});

test('with automatic start off, the flow waits for a choice', () => {
  const state = checked(initialState({ autoStart: false }));
  assert.equal(state.phase, PHASE.WAITING);
  assert.equal(state.held, 'choice');
});

test('a resumed installation needs no countdown and keeps the saved settings', () => {
  const state = checked(initialState({ resume: true }));
  assert.equal(state.phase, PHASE.INSTALLING);
  assert.equal(state.recommended, false);
});

test('customize during the checks or the countdown means waiting', () => {
  const duringChecks = next(initialState(), { type: 'hold', reason: 'customize' }, NOW);
  assert.equal(duringChecks.phase, PHASE.CHECKING);
  assert.equal(checked(duringChecks).phase, PHASE.WAITING);
  assert.equal(checked(duringChecks).held, 'customize');

  const countdown = checked(initialState());
  const held = next(countdown, { type: 'hold', reason: 'customize' }, NOW);
  assert.equal(held.phase, PHASE.WAITING);
  assert.equal(held.endsAt, null);
});

test('customize from waiting for a choice takes the user through the pages', () => {
  const waiting = checked(initialState({ autoStart: false }));
  const customizing = next(waiting, { type: 'hold', reason: 'customize' }, NOW);
  assert.equal(customizing.phase, PHASE.WAITING);
  assert.equal(customizing.held, 'customize');
});

test('the countdown ending installs the recommended settings; a stray timeout does nothing', () => {
  const countdown = checked(initialState());
  const state = next(countdown, { type: 'timeout' }, NOW);
  assert.equal(state.phase, PHASE.INSTALLING);
  assert.equal(state.recommended, true);

  const waiting = checked(initialState({ autoStart: false }));
  assert.equal(next(waiting, { type: 'timeout' }, NOW), waiting);
});

test('Install now uses the recommended settings only from the countdown', () => {
  const countdown = checked(initialState());
  assert.equal(next(countdown, { type: 'installNow' }, NOW).recommended, true);

  const waiting = checked(initialState({ autoStart: false }));
  const fromWaiting = next(waiting, { type: 'installNow', includeElevated: true }, NOW);
  assert.equal(fromWaiting.phase, PHASE.INSTALLING);
  assert.equal(fromWaiting.recommended, false);
  assert.equal(fromWaiting.includeElevated, true);
});

test('approval of the elevated steps is remembered in any phase but manual', () => {
  const installing = next(checked(initialState()), { type: 'timeout' }, NOW);
  assert.equal(next(installing, { type: 'approveElevated' }, NOW).includeElevated, true);
});

test('manual ends the flow, but not in the middle of an installation', () => {
  const countdown = checked(initialState());
  assert.equal(next(countdown, { type: 'manual' }, NOW).phase, PHASE.MANUAL);

  const installing = next(checked(initialState()), { type: 'timeout' }, NOW);
  assert.equal(next(installing, { type: 'manual' }, NOW), installing);
});

test('an installation ends finished, or waiting when it was cancelled', () => {
  const installing = next(checked(initialState()), { type: 'timeout' }, NOW);
  const finished = next(installing, { type: 'installEnded', result: { envOk: true } }, NOW);
  assert.equal(finished.phase, PHASE.FINISHED);
  assert.equal(next(finished, { type: 'installNow' }, NOW).phase, PHASE.INSTALLING);

  const cancelled = next(installing, { type: 'installEnded', cancelled: true, result: null }, NOW);
  assert.equal(cancelled.phase, PHASE.WAITING);
  assert.equal(cancelled.held, 'cancelled');
});

// ---------------------------------------------------------------------------
// The controller
// ---------------------------------------------------------------------------

test('an unattended first launch installs with what the test found, then starts the backend', async (t) => {
  const h = harness(t);
  const { controller, runner, file, applied, starts } = h;
  await startAndInstall(h);
  const done = await waitFor(controller, inPhase(PHASE.FINISHED));

  assert.deepEqual(runner.runs, [false]);
  assert.equal(done.result.envOk, true);
  // The Windows proxy that worked is saved, so the downloads use it.
  assert.deepEqual(
    applied.map((change) => [change.path, change.value]),
    [
      ['httpProxy', PROXY],
      ['httpsProxy', PROXY],
      ['noProxy', 'localhost,127.0.0.1'],
    ]
  );
  // ...but the window only ever sees it masked.
  assert.equal(done.network.chosen.httpsProxy, 'http://***@proxy.example.com:911');
  assert.equal(starts(), 1);

  const record = readRecord(file);
  assert.equal(record.stage, 'done');
  assert.equal(typeof record.completedAt, 'string');
});

test('installation waits for the countdown', async (t) => {
  const { controller, runner, timers } = harness(t);
  controller.start();
  await waitFor(controller, inPhase(PHASE.COUNTDOWN));

  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, COUNTDOWN_SECONDS * 1000);
  assert.deepEqual(runner.runs, []);

  timers[0].fn();
  await waitFor(controller, inPhase(PHASE.FINISHED));
  assert.deepEqual(runner.runs, [false]);
});

test('a failed network test waits for the user, who installs with their own settings', async (t) => {
  const { controller, runner, timers, applied } = harness(t, { network: FAILED_NETWORK });
  controller.start();
  const waiting = await waitFor(controller, inPhase(PHASE.WAITING));

  assert.equal(waiting.held, 'network');
  assert.equal(waiting.network.ok, false);
  assert.deepEqual(timers, []);
  assert.deepEqual(runner.runs, []);

  controller.send('installNow');
  await waitFor(controller, inPhase(PHASE.FINISHED));
  assert.deepEqual(runner.runs, [false]);
  assert.deepEqual(applied, []);
});

test('customize stops the countdown, and the later installation keeps the user\'s settings', async (t) => {
  const { controller, runner, timers, applied, file } = harness(t);
  controller.start();
  await waitFor(controller, inPhase(PHASE.COUNTDOWN));

  controller.send('customize');
  assert.equal(controller.snapshot().phase, PHASE.WAITING);
  assert.equal(timers[0].cleared, true);
  assert.equal(readRecord(file).mode, 'custom');

  controller.send('installNow');
  await waitFor(controller, inPhase(PHASE.FINISHED));
  assert.deepEqual(runner.runs, [false]);
  assert.deepEqual(applied, []);
});

test('with automatic start off, nothing installs until asked', async (t) => {
  const { controller, runner } = harness(t);
  controller.start({ autoStart: false });
  const waiting = await waitFor(controller, inPhase(PHASE.WAITING));

  assert.equal(waiting.held, 'choice');
  assert.deepEqual(runner.runs, []);
});

test('set up manually ends the flow, and later launches stay manual', async (t) => {
  const { controller, runner, file } = harness(t);
  controller.start();
  await waitFor(controller, inPhase(PHASE.COUNTDOWN));

  controller.send('manual');
  assert.equal(controller.snapshot().phase, PHASE.MANUAL);
  assert.deepEqual(runner.runs, []);
  assert.equal(readRecord(file).mode, 'manual');
  assert.equal(launchPlan({ record: readRecord(file), envOk: false }), 'manual');
});

test('an installation interrupted by closing the app resumes without a countdown', async (t) => {
  const { controller, runner, applied, timers } = harness(t, { record: { stage: 'installing' } });
  controller.start();
  await waitFor(controller, inPhase(PHASE.FINISHED));

  assert.equal(timers.length, 0);
  assert.deepEqual(runner.runs, [false]);
  assert.deepEqual(applied, []);
});

test('a setup step started by hand during the countdown stops it', async (t) => {
  const { controller, runner } = harness(t);
  controller.start();
  await waitFor(controller, inPhase(PHASE.COUNTDOWN));

  runner.busy = true;
  runner.emit('changed');
  const waiting = controller.snapshot();
  assert.equal(waiting.phase, PHASE.WAITING);
  assert.equal(waiting.held, 'setup');
});

test('approval given during the installation runs the elevated steps next', async (t) => {
  let controller;
  const runner = new FakeRunner({
    onRun: (_runner, round) => {
      if (round === 1) controller.send('approveElevated');
    },
  });
  const h = harness(t, { runner });
  ({ controller } = h);
  await startAndInstall(h);
  await waitFor(controller, inPhase(PHASE.FINISHED));

  assert.deepEqual(runner.runs, [false, true]);
});

test('a cancelled installation waits, starts nothing and leaves the wizard to come back', async (t) => {
  const runner = new FakeRunner({
    onRun: (self) => {
      self.autoCancelled = true;
    },
  });
  const h = harness(t, { runner });
  const { controller, file, starts } = h;
  await startAndInstall(h);
  const waiting = await waitFor(controller, inPhase(PHASE.WAITING));

  assert.equal(waiting.held, 'cancelled');
  assert.equal(starts(), 0);
  const record = readRecord(file);
  assert.equal(record.stage, 'welcome');
  assert.equal(record.completedAt, null);
});

test('an installation that leaves the environment broken is retried at the next launch', async (t) => {
  const h = harness(t, { runner: new FakeRunner({ venvAfter: 'missing' }) });
  const { controller, file, starts } = h;
  await startAndInstall(h);
  const done = await waitFor(controller, inPhase(PHASE.FINISHED));

  assert.equal(done.result.envOk, false);
  assert.equal(starts(), 0);
  assert.equal(readRecord(file).stage, 'installing');
  assert.equal(launchPlan({ record: readRecord(file), envOk: false }), 'wizard');
});

test('markCompleted() writes the record once', (t) => {
  const { controller, file } = harness(t);
  controller.markCompleted();
  const first = readRecord(file).completedAt;
  assert.equal(typeof first, 'string');

  controller.markCompleted();
  assert.equal(readRecord(file).completedAt, first);
  assert.equal(controller.snapshot().active, false);
});

test('only the known choices are accepted', (t) => {
  const { controller } = harness(t);
  assert.throws(() => controller.send('format-disk'), /Unknown first-run choice/);
});

test('the snapshot carries the queue as it stood when the installation started', async (t) => {
  const h = harness(t);
  const { controller } = h;
  assert.equal(controller.snapshot().plan ?? null, null);
  await startAndInstall(h);
  const done = await waitFor(controller, inPhase(PHASE.FINISHED));
  assert.deepEqual(done.plan, [{ stepId: 'venv', actionId: 'create', kind: 'auto' }]);
});

test('closing the app mid-installation keeps the record, so the next launch resumes', async (t) => {
  let controller;
  const runner = new FakeRunner({
    onRun: (self) => {
      // What the close confirmation does: mark the exit, then cancel.
      controller.stopForExit();
      self.autoCancelled = true;
    },
  });
  const h = harness(t, { runner });
  ({ controller } = h);
  const file = controller.recordFile();
  await startAndInstall(h);
  await waitFor(controller, inPhase(PHASE.WAITING));

  assert.equal(readRecord(file).stage, 'installing');
  assert.equal(launchPlan({ record: readRecord(file), envOk: false }), 'wizard');
});

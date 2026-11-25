// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// runAutomatic() is what prepares a fresh machine on first launch. These tests
// seed the state a fresh machine would check to and stub runStep and runAdmin,
// so nothing is installed: what is under test is which steps run, in what
// order, and which are left for the Setup screen.

const test = require('node:test');
const assert = require('node:assert/strict');

const { SetupRunner } = require('../../electron/services/setup-runner.cjs');
const registry = require('../../electron/services/registry.cjs');

// Which features count as enabled, pinned per test instead of read from the
// developer's config.yaml. setup-runner looks the rule up on every call, so
// replacing it here reaches the DL Streamer step.
const realEffectivelyEnabled = registry.effectivelyEnabled;
let featureOverrides = {};
registry.effectivelyEnabled = (id) => (id in featureOverrides ? featureOverrides[id] : realEffectivelyEnabled(id));

const quietLogs = { note() { }, openSink() { }, closeSink() { } };

// Never the real helper: it would raise a UAC prompt.
const noElevation = () => {
  throw new Error('The administrator helper must not run in tests.');
};

// A fresh machine: no Python, FFmpeg or environment, long paths off, no DL
// Streamer. Every other step is satisfied.
const FRESH = {
  longPaths: { status: 'missing', repair: 'enable' },
  python: { status: 'missing', repair: 'install' },
  ffmpeg: { status: 'missing', repair: 'install' },
  dlStreamer: { status: 'missing', repair: 'install' },
  venv: { status: 'missing', repair: 'create' },
};

function runner({ fail = [], onRun, features = {} } = {}) {
  featureOverrides = { video_analytics: true, ...features };
  const setup = new SetupRunner(quietLogs, { adminTasks: noElevation });
  for (const id of setup.state.keys()) {
    setup.state.set(id, { ...setup.state.get(id), status: 'ok', repair: null });
  }
  for (const [id, state] of Object.entries(FRESH)) {
    setup.state.set(id, { ...setup.state.get(id), ...state });
  }

  const calls = [];
  const ran = (stepId, actionId) => {
    calls.push(`${stepId}:${actionId}`);
    onRun?.(stepId, setup);
    if (fail.includes(stepId)) {
      setup.state.set(stepId, { ...setup.state.get(stepId), status: 'failed', repair: null });
      throw new Error(`${stepId} failed`);
    }
    setup.state.set(stepId, { ...setup.state.get(stepId), status: 'ok', repair: null });
    return setup.state.get(stepId);
  };
  setup.runStep = async (stepId, actionId) => ran(stepId, actionId);

  // The administrator tasks, which a run hands over together: one batch is
  // one UAC prompt.
  const batches = [];
  setup.runAdmin = async (entries) => {
    batches.push(entries.map(({ step }) => step.id));
    return entries.map(({ step, action }) => {
      try {
        ran(step.id, action.id);
        return { step, ok: true, skipped: false, error: null };
      } catch (error) {
        return { step, ok: false, skipped: false, error };
      }
    });
  };
  return { setup, calls, batches };
}

test('a fresh machine gets Python, FFmpeg and the environment, in that order', async () => {
  const { setup, calls } = runner();
  const result = await setup.runAutomatic();

  assert.deepEqual(calls, ['python:install', 'ffmpeg:install', 'venv:create']);
  assert.deepEqual(result.ran, ['python', 'ffmpeg', 'venv']);
  assert.deepEqual(result.failed, []);
  assert.deepEqual(result.blocked, []);
});

test('steps that raise a UAC prompt are left for the Setup screen', async () => {
  const { setup, calls } = runner();
  const result = await setup.runAutomatic();

  assert.deepEqual(result.manual, ['longPaths', 'dlStreamer']);
  assert.ok(!calls.some((call) => call.startsWith('longPaths') || call.startsWith('dlStreamer')));
});

test('a destructive repair is never run unattended', async () => {
  const { setup, calls } = runner();
  // An environment built on the wrong interpreter can only be recreated.
  setup.state.set('venv', { ...setup.state.get('venv'), status: 'outdated', repair: 'recreate' });
  const result = await setup.runAutomatic();

  assert.ok(!calls.includes('venv:recreate'));
  assert.ok(result.manual.includes('venv'));
});

test('a failure does not stop unrelated steps, but blocks the ones that need it', async () => {
  const { setup, calls } = runner({ fail: ['python'] });
  const result = await setup.runAutomatic();

  // FFmpeg does not need Python; the environment does.
  assert.deepEqual(calls, ['python:install', 'ffmpeg:install']);
  assert.deepEqual(result.failed.map((entry) => entry.id), ['python']);
  assert.deepEqual(result.blocked, ['venv']);
});

test('cancel stops the run instead of moving to the next step', async () => {
  const { setup, calls } = runner({
    onRun: (stepId, self) => {
      if (stepId === 'python') self.cancel();
    },
  });
  await setup.runAutomatic();

  assert.deepEqual(calls, ['python:install']);
  assert.equal(setup.autoCancelled, true);
});

test('a new run clears an earlier cancel', async () => {
  const { setup, calls } = runner();
  setup.cancel();
  await setup.runAutomatic();

  assert.deepEqual(calls, ['python:install', 'ffmpeg:install', 'venv:create']);
});

test('working stays true for the whole run, including between steps', async () => {
  const seen = [];
  const { setup } = runner({ onRun: (stepId, self) => seen.push([stepId, self.working]) });
  // runStep is stubbed, so `busy` never goes up: what is seen is autoRunning.
  const changes = [];
  setup.on('changed', () => changes.push(setup.working));

  await setup.runAutomatic();

  assert.deepEqual(seen, [['python', true], ['ffmpeg', true], ['venv', true]]);
  assert.equal(setup.working, false);
  // Announced both ways, so whatever gates on it can re-read it.
  assert.deepEqual(changes, [true, false]);
});

test('working clears even when a step fails', async () => {
  const { setup } = runner({ fail: ['python', 'ffmpeg'] });
  await setup.runAutomatic();
  assert.equal(setup.working, false);
});

test('plan() lists the repairs in step order, each with how it runs', () => {
  const { setup } = runner();
  assert.deepEqual(setup.plan(), [
    { stepId: 'longPaths', actionId: 'enable', kind: 'elevated' },
    { stepId: 'python', actionId: 'install', kind: 'auto' },
    { stepId: 'ffmpeg', actionId: 'install', kind: 'auto' },
    { stepId: 'dlStreamer', actionId: 'install', kind: 'elevated' },
    { stepId: 'venv', actionId: 'create', kind: 'auto' },
  ]);
});

test('plan() marks a destructive repair as manual', () => {
  const { setup } = runner();
  setup.state.set('venv', { ...setup.state.get('venv'), status: 'outdated', repair: 'recreate' });
  assert.equal(setup.plan().find((entry) => entry.stepId === 'venv').kind, 'manual');
});

test('with approval, plan() and the run put the elevated steps first', async () => {
  const { setup, calls } = runner();
  assert.deepEqual(
    setup.plan({ includeElevated: true }).map((entry) => entry.stepId),
    ['longPaths', 'dlStreamer', 'python', 'ffmpeg', 'venv']
  );

  const result = await setup.runAutomatic({ includeElevated: true });

  // The prompt comes while the person who approved it is still there.
  assert.deepEqual(calls, ['longPaths:enable', 'dlStreamer:install', 'python:install', 'ffmpeg:install', 'venv:create']);
  assert.deepEqual(result.ran, ['longPaths', 'dlStreamer', 'python', 'ffmpeg', 'venv']);
  assert.deepEqual(result.manual, []);
});

test('the approved administrator tasks run together, behind one prompt', async () => {
  const { setup, batches } = runner();
  await setup.runAutomatic({ includeElevated: true });
  assert.deepEqual(batches, [['longPaths', 'dlStreamer']]);
});

test('an administrator task that fails is reported like any other step', async () => {
  const { setup, calls } = runner({ fail: ['dlStreamer'] });
  const result = await setup.runAutomatic({ includeElevated: true });
  assert.deepEqual(result.failed, [{ id: 'dlStreamer', error: 'dlStreamer failed' }]);
  assert.ok(result.ran.includes('longPaths'));
  // Nothing else needs DL Streamer, so the rest still runs.
  assert.ok(calls.includes('venv:create'));
});

test('approval still never runs a destructive repair', async () => {
  const { setup, calls } = runner();
  setup.state.set('venv', { ...setup.state.get('venv'), status: 'outdated', repair: 'recreate' });
  const result = await setup.runAutomatic({ includeElevated: true });

  assert.ok(!calls.includes('venv:recreate'));
  assert.deepEqual(result.manual, ['venv']);
});

test('without video analytics, DL Streamer is neither planned nor reported', async () => {
  const { setup, calls } = runner({ features: { video_analytics: false } });

  assert.ok(!setup.plan().some((entry) => entry.stepId === 'dlStreamer'));
  const result = await setup.runAutomatic({ includeElevated: true });
  assert.ok(!calls.includes('dlStreamer:install'));
  assert.ok(!result.manual.includes('dlStreamer'));
  // And the Setup screen does not list it.
  assert.ok(!setup.snapshot().some((step) => step.id === 'dlStreamer'));
});

test('the setup data says how each repair runs, so screens need no step ids', () => {
  const { setup } = runner();
  setup.state.set('venv', { ...setup.state.get('venv'), status: 'outdated', repair: 'recreate' });
  const byId = Object.fromEntries(setup.snapshot().map((step) => [step.id, step]));

  assert.equal(byId.longPaths.repairKind, 'elevated');
  assert.equal(byId.dlStreamer.repairKind, 'elevated');
  assert.equal(byId.python.repairKind, 'auto');
  assert.equal(byId.venv.repairKind, 'manual');
  assert.equal(byId.os.repairKind, null);
  assert.equal(byId.longPaths.actions.find((action) => action.id === 'enable').elevates, true);
  assert.equal(byId.python.actions.find((action) => action.id === 'install').elevates, false);
});

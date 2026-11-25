// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The first-run wizard's flow, owned by the main process.
//
// The window only shows this flow and sends the user's choices. The countdown
// runs here, so it ends — and installation starts — whether or not the window
// ever loaded: nothing in the renderer can stall an unattended first launch.
// (An earlier version installed at once while no wizard had said it was on
// screen; but the checks can finish before the window has loaded, which made
// the countdown, and the chance to customize, depend on a race.)
//
// The record, state/first-run.json, holds onboarding facts only. It never
// claims the machine is ready: the Setup screen derives that from live checks,
// because a stored flag goes stale when the environment is deleted or moved.

const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');

const { evaluate, needsInstallers, recommendedChanges, masked, maskProxy } = require('./network-probe.cjs');

const SCHEMA = 1;
const STAGES = ['welcome', 'customize', 'installing', 'done'];
const MODES = ['recommended', 'custom', 'manual'];

const PHASE = {
  CHECKING: 'checking',
  COUNTDOWN: 'countdown',
  WAITING: 'waiting',
  INSTALLING: 'installing',
  FINISHED: 'finished',
  MANUAL: 'manual',
};

// Seconds before the recommended installation starts by itself: long enough
// to read the page and choose Customize. Never forever when the checks pass:
// nobody may be there (design, D6). A failed network test or a nearly full
// disk stops the flow instead, because installing then fails, or fills the
// disk, partway through.
const COUNTDOWN_SECONDS = 15;

// ---------------------------------------------------------------------------
// Record
// ---------------------------------------------------------------------------

/** The record, or null when it is missing or not one this version wrote. */
function readRecord(file) {
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
  if (!data || data.schema !== SCHEMA || !STAGES.includes(data.stage) || !MODES.includes(data.mode)) return null;
  if (data.completedAt !== null && typeof data.completedAt !== 'string') return null;
  return { schema: SCHEMA, stage: data.stage, mode: data.mode, completedAt: data.completedAt };
}

/** Write through a temporary file, so a crash cannot leave half a record. */
function writeRecord(file, record) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(record, null, 2)}\n`);
  fs.renameSync(temporary, file);
}

/**
 * What a windowed launch does (design, Section 5).
 *
 * - `ready`: the Python environment is OK, so start the backend.
 * - `repair`: the user completed the wizard and the environment broke later;
 *   the automatic repair runs, as before the wizard existed.
 * - `manual`: the user chose to set up by hand; check, install nothing.
 * - `wizard`: everything else.
 */
function launchPlan({ record, envOk }) {
  if (envOk) return 'ready';
  if (record?.completedAt) return 'repair';
  if (record?.mode === 'manual') return 'manual';
  return 'wizard';
}

// ---------------------------------------------------------------------------
// Flow
// ---------------------------------------------------------------------------

function initialState({ resume = false, autoStart = true } = {}) {
  return {
    phase: PHASE.CHECKING,
    resume,
    autoStart,
    // Why the flow waits instead of counting down: 'choice' (automatic start
    // is off), 'customize', 'network' (the network test failed), 'disk' (the
    // disk is nearly full), 'setup' (someone started a setup step by hand) or
    // 'cancelled'.
    held: null,
    endsAt: null,
    network: null,
    diskLow: false,
    // Whether the installation uses the recommended settings, which the
    // controller then writes before starting.
    recommended: false,
    includeElevated: false,
    result: null,
  };
}

function installing(state, { recommended }) {
  return { ...state, phase: PHASE.INSTALLING, recommended, held: null, endsAt: null };
}

/** The flow as a pure function of its state, an event and the time. */
function next(state, event, now) {
  switch (event.type) {
    case 'checked': {
      if (state.phase !== PHASE.CHECKING) return state;
      const checked = { ...state, network: event.network ?? null, diskLow: !!event.diskLow };
      // The user already agreed to install, before the app was closed.
      if (state.resume) return installing(checked, { recommended: false });
      if (state.held) return { ...checked, phase: PHASE.WAITING };
      // The downloads cannot get through, or the test could not run: wait for
      // someone to fix the network rather than install into a failure.
      if (!checked.network?.ok) return { ...checked, phase: PHASE.WAITING, held: 'network' };
      // Running out of space mid-download is the hardest failure to recover
      // from: installing takes a deliberate yes.
      if (checked.diskLow) return { ...checked, phase: PHASE.WAITING, held: 'disk' };
      if (!state.autoStart) return { ...checked, phase: PHASE.WAITING, held: 'choice' };
      return { ...checked, phase: PHASE.COUNTDOWN, endsAt: now + COUNTDOWN_SECONDS * 1000 };
    }

    case 'timeout':
      return state.phase === PHASE.COUNTDOWN ? installing(state, { recommended: true }) : state;

    case 'hold':
      if (state.phase === PHASE.CHECKING) return state.held ? state : { ...state, held: event.reason };
      if (state.phase === PHASE.COUNTDOWN) {
        return { ...state, phase: PHASE.WAITING, held: event.reason, endsAt: null };
      }
      // Already waiting, for example with automatic start off: Customize
      // still has to take the user through the pages.
      if (state.phase === PHASE.WAITING) return state.held === event.reason ? state : { ...state, held: event.reason };
      return state;

    case 'installNow': {
      const includeElevated = state.includeElevated || !!event.includeElevated;
      // From the countdown, nothing was customized: the recommended settings.
      if (state.phase === PHASE.COUNTDOWN) return installing({ ...state, includeElevated }, { recommended: true });
      if (state.phase === PHASE.WAITING || state.phase === PHASE.FINISHED) {
        return installing({ ...state, includeElevated }, { recommended: false });
      }
      return state;
    }

    // Approval can come during the installation, from the card on the Install
    // page; the controller runs the elevated steps when their turn comes.
    case 'approveElevated':
      return state.includeElevated || state.phase === PHASE.MANUAL ? state : { ...state, includeElevated: true };

    case 'manual':
      if (![PHASE.CHECKING, PHASE.COUNTDOWN, PHASE.WAITING].includes(state.phase)) return state;
      return { ...state, phase: PHASE.MANUAL, held: null, endsAt: null };

    case 'installEnded':
      if (state.phase !== PHASE.INSTALLING) return state;
      if (event.cancelled) return { ...state, phase: PHASE.WAITING, held: 'cancelled', result: event.result ?? null };
      return { ...state, phase: PHASE.FINISHED, result: event.result ?? null };

    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

const describeCandidate = (candidate) =>
  candidate.httpsProxy || candidate.httpProxy
    ? `the ${candidate.source} proxy ${maskProxy(candidate.httpsProxy || candidate.httpProxy)}`
    : 'a direct connection';

const environmentOk = (steps) => {
  const venv = steps.find((step) => step.id === 'venv');
  return venv?.status === 'ok' || venv?.status === 'outdated';
};

class FirstRunController extends EventEmitter {
  /**
   * @param {object} deps
   * @param {() => string} deps.recordFile
   * @param {import('./setup-runner.cjs').SetupRunner} deps.setupRunner
   * @param {() => Promise<object>} deps.testNetwork  network-probe test()
   * @param {() => string} deps.configuredHub  'huggingface' or 'modelscope'
   * @param {(changes: object[]) => void} deps.applyChanges  config-store apply()
   * @param {() => void} deps.startBackend
   */
  constructor({
    recordFile,
    setupRunner,
    testNetwork,
    configuredHub,
    applyChanges,
    startBackend,
    log = () => {},
    now = () => Date.now(),
    setTimer = setTimeout,
    clearTimer = clearTimeout,
  }) {
    super();
    Object.assign(this, { recordFile, setupRunner, testNetwork, configuredHub, applyChanges, startBackend, log, now });
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.state = null;
    this.record = undefined;
    this.timer = null;
    this.installingNow = false;
    this.exiting = false;
    // The queue as it stood when the installation started, for the Install
    // page: steps drop out of plan() as they succeed.
    this.installPlan = null;
    this.recommended = [];
    this.onSetupChanged = this.onSetupChanged.bind(this);
  }

  currentRecord() {
    if (this.record === undefined) this.record = readRecord(this.recordFile());
    return this.record;
  }

  saveRecord(patch) {
    this.record = { schema: SCHEMA, stage: 'welcome', mode: 'recommended', completedAt: null, ...this.currentRecord(), ...patch };
    try {
      writeRecord(this.recordFile(), this.record);
    } catch (error) {
      // Without the record the next launch decides from the environment
      // alone, which is safe; it is not worth failing the flow over.
      this.log(`Could not save the first-run record: ${error.message}`);
    }
  }

  launchPlan({ envOk }) {
    return launchPlan({ record: this.currentRecord(), envOk });
  }

  /** The Python environment is OK: the wizard has nothing left to do, ever. */
  markCompleted() {
    if (this.currentRecord()?.completedAt) return;
    this.saveRecord({ stage: 'done', completedAt: new Date(this.now()).toISOString() });
  }

  /** Begin the flow; launchPlan() said `wizard`. */
  start({ autoStart = true } = {}) {
    if (this.state) return;
    const resume = this.currentRecord()?.stage === 'installing';
    this.state = initialState({ resume, autoStart });
    this.saveRecord({ stage: resume ? 'installing' : 'welcome' });
    this.setupRunner.on('changed', this.onSetupChanged);
    this.emitChanged();
    this.runChecks().catch((error) => {
      this.log(`First-run checks failed: ${error.message}`);
      this.dispatch({ type: 'checked', network: null, diskLow: false });
    });
  }

  /** A choice from the window. Only these names cross IPC. */
  send(choice, options = {}) {
    switch (choice) {
      case 'customize':
        this.dispatch({ type: 'hold', reason: 'customize' });
        break;
      case 'installNow':
        this.dispatch({ type: 'installNow', includeElevated: options?.includeElevated === true });
        break;
      case 'approveElevated':
        this.dispatch({ type: 'approveElevated' });
        break;
      case 'manual':
        this.dispatch({ type: 'manual' });
        break;
      default:
        throw new Error(`Unknown first-run choice: ${String(choice).slice(0, 32)}`);
    }
    return this.snapshot();
  }

  snapshot() {
    const record = this.currentRecord();
    const view = record ? { stage: record.stage, mode: record.mode, completedAt: record.completedAt } : null;
    if (!this.state) return { active: false, record: view };
    const { phase, held, endsAt, diskLow, recommended, includeElevated, network, result } = this.state;
    return {
      active: true,
      phase,
      held,
      endsAt,
      diskLow,
      recommended,
      includeElevated,
      network,
      plan: this.installPlan,
      result,
      record: view,
    };
  }

  emitChanged() {
    this.emit('changed', this.snapshot());
  }

  dispatch(event) {
    const before = this.state;
    if (!before) return;
    const after = next(before, event, this.now());
    if (after === before) return;
    this.state = after;
    this.transition(before, after);
    this.emitChanged();
  }

  transition(before, after) {
    if (before.phase === PHASE.COUNTDOWN && after.phase !== PHASE.COUNTDOWN) this.clearCountdown();
    if (after.phase === PHASE.COUNTDOWN && before.phase !== PHASE.COUNTDOWN) {
      this.timer = this.setTimer(() => this.dispatch({ type: 'timeout' }), Math.max(0, after.endsAt - this.now()));
    }
    if (after.held === 'customize' && before.held !== 'customize') this.saveRecord({ stage: 'customize', mode: 'custom' });
    if (after.phase === PHASE.MANUAL && before.phase !== PHASE.MANUAL) {
      this.saveRecord({ stage: 'welcome', mode: 'manual' });
      this.log('First run: set up manually; nothing is installed automatically.');
    }
    if (after.phase === PHASE.INSTALLING && before.phase !== PHASE.INSTALLING) {
      this.install(after).catch((error) => this.log(`First-run installation failed: ${error.message}`));
    }
  }

  clearCountdown() {
    if (this.timer) this.clearTimer(this.timer);
    this.timer = null;
  }

  /**
   * The app is closing during the installation. Its setup step is about to be
   * cancelled, but unlike Cancel this is not the user changing their mind:
   * the record keeps `installing`, so the next launch picks up where this one
   * stopped (design, Section 7.6).
   */
  stopForExit() {
    this.exiting = true;
  }

  /** A setup step started by hand during the checks or the countdown. */
  onSetupChanged() {
    if (!this.state || this.installingNow || !this.setupRunner.busy) return;
    if (this.state.phase === PHASE.CHECKING || this.state.phase === PHASE.COUNTDOWN) {
      this.dispatch({ type: 'hold', reason: 'setup' });
    }
  }

  async runChecks() {
    let raw = null;
    const [steps] = await Promise.all([
      this.setupRunner.checkAll(),
      this.testNetwork().then(
        (value) => {
          raw = value;
        },
        (error) => this.log(`The network test could not run: ${error.message}`)
      ),
    ]);

    const evaluation = evaluate(raw?.chosen?.results ?? [], {
      hub: this.configuredHub(),
      needInstallers: needsInstallers(this.setupRunner.plan()),
    });
    this.recommended = raw ? recommendedChanges(raw, evaluation) : [];
    this.logNetwork(raw, evaluation);

    const network = { ...evaluation, chosen: masked(raw?.chosen), tried: (raw?.tried ?? []).map(masked) };
    const diskLow = steps.some((step) => step.id === 'disk' && step.status === 'warn');
    this.dispatch({ type: 'checked', network, diskLow });
  }

  logNetwork(raw, evaluation) {
    if (!raw) return;
    if (raw.chosen) {
      this.log(`Network: ${describeCandidate(raw.chosen)} reaches the Python package hosts.`);
    } else {
      const causes = raw.tried
        .map((entry) => `${describeCandidate(entry)}: ${entry.results.find((result) => !result.ok)?.cause ?? 'failed'}`)
        .join('; ');
      this.log(`Network: nothing tried reaches the Python package hosts (${causes}).`);
    }
    if (evaluation.suggestedHub) {
      this.log(`Network: ${evaluation.hub} cannot be reached and ${evaluation.suggestedHub} can, so models will come from ${evaluation.suggestedHub}.`);
    }
  }

  async install(state) {
    this.installingNow = true;
    this.installPlan = this.setupRunner.plan({ includeElevated: state.includeElevated });
    this.saveRecord({ stage: 'installing' });

    if (state.recommended && this.recommended.length) {
      try {
        this.applyChanges(this.recommended);
        this.log('Saved the network settings the test found.');
      } catch (error) {
        this.log(`Could not save the network settings the test found: ${error.message}`);
      }
    }

    let result = { ran: [], failed: [], blocked: [], manual: [] };
    try {
      let elevatedRan = false;
      for (;;) {
        const includeElevated = this.state.includeElevated;
        const round = await this.setupRunner.runAutomatic({ includeElevated });
        result = {
          ran: [...result.ran, ...round.ran],
          failed: [...result.failed, ...round.failed],
          blocked: round.blocked,
          manual: round.manual,
        };
        elevatedRan = elevatedRan || includeElevated;
        // Approved while the first round ran: the runner does one step at a
        // time, so their turn is now.
        if (this.setupRunner.autoCancelled || !this.state.includeElevated || elevatedRan) break;
      }
    } catch (error) {
      result.failed.push({ id: null, error: error.message });
    } finally {
      this.installingNow = false;
    }

    const cancelled = this.setupRunner.autoCancelled;
    const envOk = environmentOk(this.setupRunner.snapshot());
    if (cancelled) {
      if (!this.exiting) this.saveRecord({ stage: 'welcome' });
    } else if (envOk) {
      this.markCompleted();
    }
    // Otherwise the record still says `installing`, so the next launch tries
    // again without asking: the user already agreed.

    this.dispatch({ type: 'installEnded', cancelled, result: { ...result, envOk } });
    if (!cancelled && envOk) this.startBackend();
  }
}

module.exports = {
  SCHEMA,
  PHASE,
  COUNTDOWN_SECONDS,
  readRecord,
  writeRecord,
  launchPlan,
  initialState,
  next,
  FirstRunController,
};

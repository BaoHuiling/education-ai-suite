// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Model downloads report byte-level progress as "@@download {json}" lines
// (utils/download_progress.py). Those lines leave the log and end up on the
// service or setup step that printed them, for the screens' progress bars.

const test = require('node:test');
const assert = require('node:assert/strict');
const os = require('os');
const path = require('path');

const { PREFIX, parseProgressLine, isProgressLine } = require('../../electron/services/download-progress.cjs');
const { LogStore } = require('../../electron/services/log-store.cjs');
const { ServiceManager } = require('../../electron/services/process-manager.cjs');
const { SetupRunner } = require('../../electron/services/setup-runner.cjs');

const event = (fields = {}) => ({
  repo: 'org/model',
  hub: 'huggingface',
  state: 'running',
  done: 10,
  total: 100,
  speed: 5.5,
  files: 1,
  fileTotal: 4,
  ...fields,
});
const line = (fields) => PREFIX + JSON.stringify(event(fields));

// --- parsing ---------------------------------------------------------------

test('a progress line is read field by field', () => {
  assert.deepEqual(parseProgressLine(line()), event());
  assert.deepEqual(parseProgressLine(line({ speed: null, fileTotal: null })), event({ speed: null, fileTotal: null }));
  // Only the fields listed are kept.
  assert.deepEqual(parseProgressLine(line({ extra: 'x' })), event());
});

test('what a child prints is checked before it is believed', () => {
  for (const bad of [
    'Downloading org/model',
    `${PREFIX}not json`,
    `${PREFIX}null`,
    `${PREFIX}[1,2]`,
    line({ repo: '' }),
    line({ repo: 42 }),
    line({ state: 'paused' }),
    line({ done: -1 }),
    line({ done: 'lots' }),
    line({ total: null }),
  ]) {
    assert.equal(parseProgressLine(bad), null, bad);
  }
  // Repaired rather than refused.
  assert.equal(parseProgressLine(line({ done: 150, total: 100 })).total, 150);
  assert.equal(parseProgressLine(line({ speed: -3 })).speed, null);
  assert.equal(parseProgressLine(line({ files: undefined })).files, 0);
  assert.equal(parseProgressLine(line({ done: 10.7 })).done, 10);
  assert.equal(parseProgressLine(line({ repo: 'x'.repeat(500) })).repo.length, 200);
  assert.equal(parseProgressLine(line({ hub: 7 })).hub, '');
});

test('a malformed progress line is still kept out of the log', () => {
  assert.equal(isProgressLine(`${PREFIX}not json`), true);
  assert.equal(isProgressLine('[layout_detection] @@download {}'), false);
  assert.equal(isProgressLine(undefined), false);
});

// --- the log store ---------------------------------------------------------

function store() {
  const logs = new LogStore(path.join(os.tmpdir(), 'sc-download-progress-test'));
  const tapped = [];
  logs.setRoute((text) => {
    const match = /^\[(child)\] (.*)$/.exec(text);
    return match ? { id: 'child', text: match[2] } : null;
  });
  logs.setTap((id, text) => {
    if (!isProgressLine(text)) return false;
    tapped.push([id, text]);
    return true;
  });
  return { logs, tapped, texts: (id) => logs.read(id).map((entry) => entry.text) };
}

test('the tap takes progress out of the log, after routing', () => {
  const { logs, tapped, texts } = store();
  logs.write('parent', `starting\n${line()}\n[child] ${line({ repo: 'org/child' })}\n[child] loaded\n`);
  assert.deepEqual(texts('parent'), ['starting']);
  assert.deepEqual(texts('child'), ['loaded']);
  // The child's line reaches the tap as the child's, without its tag.
  assert.deepEqual(tapped, [
    ['parent', line()],
    ['child', line({ repo: 'org/child' })],
  ]);
});

test('a stderr line cannot land in the middle of a stdout one', () => {
  const { logs, tapped, texts } = store();
  // print() writes the text and the newline separately; a log record on
  // stderr can arrive in between.
  const progress = line();
  logs.write('parent', progress.slice(0, 20), 'stdout');
  logs.write('parent', 'WARNING slow mirror\n', 'stderr');
  logs.write('parent', `${progress.slice(20)}\n`, 'stdout');
  assert.deepEqual(texts('parent'), ['WARNING slow mirror']);
  assert.deepEqual(tapped, [['parent', progress]]);
});

// --- services --------------------------------------------------------------

function services() {
  const logs = new LogStore(path.join(os.tmpdir(), 'sc-download-progress-test'));
  const manager = new ServiceManager(logs);
  const changes = [];
  manager.on('changed', () => changes.push(1));
  const downloadOf = (id) => manager.snapshot().find((service) => service.id === id).download;
  return { logs, manager, changes, downloadOf };
}

test("a service's download goes on its snapshot, not into its log", () => {
  const { logs, changes, downloadOf } = services();
  assert.equal(downloadOf('backend'), null);

  logs.write('backend', `Downloading org/model@main from Hugging Face\n${line()}\n`);
  assert.deepEqual(downloadOf('backend'), event());
  assert.equal(changes.length, 1);
  assert.deepEqual(
    logs.read('backend').map((entry) => entry.text),
    ['Downloading org/model@main from Hugging Face']
  );

  // A child's output comes through the backend, tagged.
  logs.write('backend', `[layout_detection] ${line({ repo: 'org/layout', done: 50 })}\n`);
  assert.equal(downloadOf('layout-detection').repo, 'org/layout');
  assert.equal(downloadOf('backend').repo, 'org/model');

  // Nonsense is dropped without touching what is shown.
  logs.write('backend', `${PREFIX}{"repo":\n`);
  assert.deepEqual(downloadOf('backend'), event());
  assert.equal(changes.length, 2);
  assert.equal(logs.read('backend').length, 1);
});

test('a download ends with the service that runs it, and with its owner', () => {
  const { logs, manager, downloadOf } = services();
  logs.write('backend', `${line()}\n[layout_detection] ${line()}\n[chromadb] ${line()}\n`);
  assert.ok(downloadOf('backend') && downloadOf('layout-detection') && downloadOf('chroma'));

  // chroma runs under content-search, which runs under the backend.
  manager.clearDownloads('content-search');
  assert.equal(downloadOf('chroma'), null);
  assert.ok(downloadOf('backend'));

  manager.clearDownloads('backend');
  assert.equal(downloadOf('backend'), null);
  assert.equal(downloadOf('layout-detection'), null);
});

// --- setup steps -----------------------------------------------------------

test("a step's download goes on the step while it runs, not into the log", async (t) => {
  const lines = [];
  const setup = new SetupRunner({ note: (id, text) => lines.push(text), openSink() { }, closeSink() { } });
  const { step, action } = setup.find('layoutModel', 'prepare');
  const real = { enabled: step.enabled, check: step.check, run: action.run };
  t.after(() => {
    Object.assign(step, { enabled: real.enabled, check: real.check });
    action.run = real.run;
  });
  step.enabled = () => true;
  step.check = async () => ({ status: 'ok', detail: 'ready' });
  setup.refreshEnvironment = async () => undefined;

  const seen = [];
  setup.on('changed', (snapshot) => {
    const progress = snapshot.find((entry) => entry.id === 'layoutModel').progress;
    if (progress) seen.push([progress.state, progress.done]);
  });
  action.run = async (emit) => {
    emit('Downloading and converting the layout model…');
    emit(line({ done: 10 }));
    emit(`${PREFIX}not json`);
    emit(line({ state: 'done', done: 100 }));
    emit('Conversion complete');
  };

  await setup.runStep('layoutModel', 'prepare');
  assert.deepEqual(seen, [['running', 10], ['done', 100]]);
  assert.equal(setup.snapshot().find((entry) => entry.id === 'layoutModel').progress, null);
  assert.ok(lines.includes('Downloading and converting the layout model…'));
  assert.ok(lines.includes('Conversion complete'));
  assert.ok(!lines.some((text) => text.includes('@@download')));
});

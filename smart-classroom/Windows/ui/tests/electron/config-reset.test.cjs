// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { resetToShippedDefaults } = require('../../electron/services/config-reset.cjs');

const OLD_DEFAULT = 'app:\n  language: en\n';
const NEW_DEFAULT = 'app:\n  language: en\n  theme: dark\n';
const CUSTOMISED = 'app:\n  language: zh\n';
const digest = (text) => crypto.createHash('sha256').update(text).digest('hex');

function sandbox(t, { user, shipped = NEW_DEFAULT, stamp }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sc-config-reset-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = (...parts) => path.join(dir, ...parts);
  fs.writeFileSync(file('shipped.yaml'), shipped);
  if (user != null) fs.writeFileSync(file('config.yaml'), user);
  if (stamp != null) {
    fs.mkdirSync(file('state'));
    fs.writeFileSync(file('state', 'config.yaml.sha256'), `${stamp}\n`);
  }
  const options = {
    userFile: file('config.yaml'),
    defaultFile: file('shipped.yaml'),
    stampFile: file('state', 'config.yaml.sha256'),
    backupDir: file('backups'),
    label: 'config.yaml',
    now: () => new Date('2026-11-12T06:00:00.000Z'),
  };
  const read = (...parts) => (fs.existsSync(file(...parts)) ? fs.readFileSync(file(...parts), 'utf8') : null);
  const backups = () => (fs.existsSync(file('backups')) ? fs.readdirSync(file('backups')) : []);
  return { options, read, backups };
}

test('a fresh install records the stamp and leaves the copy alone', (t) => {
  const box = sandbox(t, { user: NEW_DEFAULT });
  assert.deepEqual(resetToShippedDefaults(box.options), { status: 'recorded' });
  assert.equal(box.read('config.yaml'), NEW_DEFAULT);
  assert.equal(box.read('state', 'config.yaml.sha256').trim(), digest(NEW_DEFAULT));
  assert.deepEqual(box.backups(), []);
});

test('a missing copy only records the stamp', (t) => {
  const box = sandbox(t, {});
  assert.deepEqual(resetToShippedDefaults(box.options), { status: 'recorded' });
  assert.equal(box.read('config.yaml'), null);
  assert.equal(box.read('state', 'config.yaml.sha256').trim(), digest(NEW_DEFAULT));
});

test('settings survive an update that does not change the shipped file', (t) => {
  const box = sandbox(t, { user: CUSTOMISED, stamp: digest(NEW_DEFAULT) });
  assert.deepEqual(resetToShippedDefaults(box.options), { status: 'current' });
  assert.equal(box.read('config.yaml'), CUSTOMISED);
  assert.deepEqual(box.backups(), []);
});

test('a changed shipped file replaces a customised copy and keeps the old one', (t) => {
  const box = sandbox(t, { user: CUSTOMISED, stamp: digest(OLD_DEFAULT) });
  const result = resetToShippedDefaults(box.options);
  assert.equal(result.status, 'reset');
  assert.equal(path.basename(result.backup), 'pre-update.config.yaml.2026-11-12T06-00-00-000Z.bak');
  assert.equal(fs.readFileSync(result.backup, 'utf8'), CUSTOMISED);
  assert.equal(box.read('config.yaml'), NEW_DEFAULT);
  assert.equal(box.read('state', 'config.yaml.sha256').trim(), digest(NEW_DEFAULT));
});

test('a copy made before stamps existed is replaced once', (t) => {
  const box = sandbox(t, { user: CUSTOMISED });
  assert.equal(resetToShippedDefaults(box.options).status, 'reset');
  assert.equal(box.read('config.yaml'), NEW_DEFAULT);
  assert.equal(resetToShippedDefaults(box.options).status, 'current');
  assert.equal(box.backups().length, 1);
});

test('backups fall outside the rotation config-store applies to config.yaml.*.bak', (t) => {
  const box = sandbox(t, { user: CUSTOMISED });
  const { backup } = resetToShippedDefaults(box.options);
  assert.ok(!path.basename(backup).startsWith('config.yaml.'));
});

test('the bytes of the shipped file are copied unchanged', (t) => {
  const shipped = `${String.fromCharCode(0xfeff)}app:\r\n  language: en\r\n`;
  const box = sandbox(t, { user: CUSTOMISED, shipped });
  resetToShippedDefaults(box.options);
  assert.equal(box.read('config.yaml'), shipped);
});

test('without a user data dir nothing is checked', (t) => {
  const box = sandbox(t, { user: CUSTOMISED });
  assert.deepEqual(resetToShippedDefaults({ ...box.options, stampFile: null }), { status: 'skipped' });
  assert.deepEqual(resetToShippedDefaults({ ...box.options, userFile: null }), { status: 'skipped' });
  assert.equal(box.read('config.yaml'), CUSTOMISED);
});

test('a failure leaves no stamp, so the next start tries again', (t) => {
  const box = sandbox(t, { user: CUSTOMISED });
  const blocked = { ...box.options, backupDir: path.join(box.options.defaultFile, 'not-a-dir') };
  assert.equal(resetToShippedDefaults(blocked).status, 'failed');
  assert.equal(box.read('config.yaml'), CUSTOMISED);
  assert.equal(box.read('state', 'config.yaml.sha256'), null);
});

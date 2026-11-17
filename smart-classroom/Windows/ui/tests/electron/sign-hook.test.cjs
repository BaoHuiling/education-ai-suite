// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const sign = require('../../build/sign.cjs');

function sandbox(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sc-sign-hook-'));
  const saved = process.env.SC_CODESIGN_SCRIPT;
  t.after(() => {
    if (saved === undefined) delete process.env.SC_CODESIGN_SCRIPT;
    else process.env.SC_CODESIGN_SCRIPT = saved;
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const target = path.join(dir, 'app.exe');
  fs.writeFileSync(target, 'not a PE file');
  return { dir, target };
}

test('is a no-op when SC_CODESIGN_SCRIPT is unset', async (t) => {
  const { target } = sandbox(t);
  delete process.env.SC_CODESIGN_SCRIPT;
  await sign({ path: target });
});

test('fails when SC_CODESIGN_SCRIPT points to a missing file', async (t) => {
  const { dir, target } = sandbox(t);
  process.env.SC_CODESIGN_SCRIPT = path.join(dir, 'missing.ps1');
  await assert.rejects(sign({ path: target }), /SC_CODESIGN_SCRIPT not found/);
});

test('invokes the script with the file and fails if the result is unsigned', { skip: process.platform !== 'win32' }, async (t) => {
  const { dir, target } = sandbox(t);
  const marker = path.join(dir, 'called.txt');
  const script = path.join(dir, 'fake-sign.ps1');
  fs.writeFileSync(script, `param([string]$FilePath)\nSet-Content -LiteralPath '${marker}' -Value $FilePath\nexit 0\n`);
  process.env.SC_CODESIGN_SCRIPT = script;
  await assert.rejects(sign({ path: target }), /after signing/);
  assert.equal(fs.readFileSync(marker, 'utf8').trim(), target);
});

test('propagates a non-zero exit from the signing script', { skip: process.platform !== 'win32' }, async (t) => {
  const { dir, target } = sandbox(t);
  const script = path.join(dir, 'fail-sign.ps1');
  fs.writeFileSync(script, 'exit 3\n');
  process.env.SC_CODESIGN_SCRIPT = script;
  await assert.rejects(sign({ path: target }), /Command failed/);
});

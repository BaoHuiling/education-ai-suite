// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// config-schema.cjs is a hand-kept copy of part of config.yaml, and nothing at
// run time notices when the two drift: a field whose key was renamed in the
// YAML still renders, still saves, and is silently ignored by the backend.
// These tests are what notices.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const YAML = require('yaml');

const schema = require('../../electron/services/config-schema.cjs');

const WINDOWS_ROOT = path.resolve(__dirname, '..', '..', '..');
const CONFIG_YAML = path.join(WINDOWS_ROOT, 'config.yaml');
const ZH = path.join(__dirname, '..', '..', 'src', 'i18n', 'zh.json');

const configFields = schema.FIELDS.filter((field) => field.file === schema.CONFIG);
const shipped = YAML.parseDocument(fs.readFileSync(CONFIG_YAML, 'utf8'));

// Directories never worth scanning for backend code: bundled third-party
// environments, the UI itself, and generated output.
const SKIP_DIRS = new Set([
  '.git',
  '__pycache__',
  'node_modules',
  'site-packages',
  'ui',
  'tests',
  'models',
  'build',
  'dist',
]);

function pythonSources(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name) || /^\.?venv/i.test(entry.name)) continue;
      pythonSources(path.join(dir, entry.name), out);
    } else if (entry.name.endsWith('.py')) {
      out.push(fs.readFileSync(path.join(dir, entry.name), 'utf8'));
    }
  }
  return out;
}

test('every config.yaml field in the schema exists in the shipped config.yaml', () => {
  const missing = configFields
    .filter((field) => !YAML.isScalar(shipped.getIn(field.path.split('.'), true)))
    .map((field) => field.path);
  assert.deepEqual(missing, [], `Schema fields with no scalar at that path in config.yaml: ${missing.join(', ')}`);
});

// A heuristic: the leaf key has to appear somewhere in the backend's Python.
// It cannot prove the right node is read, but a key that was renamed or
// dropped on the backend side stops appearing, which is the drift that matters.
test('every config.yaml field in the schema is read by the backend', () => {
  const source = pythonSources(WINDOWS_ROOT).join('\n');
  const unread = configFields
    .filter((field) => {
      const leaf = field.path.split('.').pop();
      return !new RegExp(`\\b${leaf}\\b`).test(source);
    })
    .map((field) => field.path);
  assert.deepEqual(unread, [], `Schema fields whose key no Python source mentions: ${unread.join(', ')}`);
});

test('appliesWhen names an enum field and values it can take', () => {
  for (const field of schema.FIELDS.filter((f) => f.appliesWhen)) {
    for (const [dependency, values] of Object.entries(field.appliesWhen)) {
      const target = schema.get(field.file, dependency);
      assert.ok(target, `${field.path}: appliesWhen names ${dependency}, which is not a schema field`);
      assert.equal(target.type, 'enum', `${field.path}: appliesWhen on ${dependency} needs an enum`);
      assert.ok(Array.isArray(values) && values.length, `${field.path}: appliesWhen.${dependency} lists no values`);
      for (const value of values) {
        assert.ok(target.options.includes(value), `${field.path}: ${dependency} has no option "${value}"`);
      }
    }
  }
});

test('every field translation in zh.json belongs to a schema field', () => {
  const known = new Set(schema.FIELDS.map((field) => field.path));
  const stale = [];
  // A node holding label/help is a field; anything else is a level of the path.
  const walk = (node, prefix) => {
    if ('label' in node || 'help' in node) {
      if (!known.has(prefix)) stale.push(prefix);
      return;
    }
    for (const [key, child] of Object.entries(node)) walk(child, prefix ? `${prefix}.${key}` : key);
  };
  walk(JSON.parse(fs.readFileSync(ZH, 'utf8')).configFields ?? {}, '');
  assert.deepEqual(stale, [], `zh.json configFields with no schema field: ${stale.join(', ')}`);
});

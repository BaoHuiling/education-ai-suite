// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The schema metadata both the setup assistant and the Configuration screen
// read: feature groups, and where a field appears during onboarding.

const test = require('node:test');
const assert = require('node:assert/strict');

const schema = require('../../electron/services/config-schema.cjs');
const config = require('../../electron/services/config-store.cjs');

test('every feature belongs to exactly one group', () => {
  const grouped = schema.FEATURE_GROUPS.flatMap((group) => group.features);
  assert.deepEqual([...grouped].sort(), Object.keys(schema.FEATURES).sort());
  assert.equal(new Set(grouped).size, grouped.length);
});

test('every group has an id, a label and a detail', () => {
  for (const group of schema.FEATURE_GROUPS) {
    assert.match(group.id, /^[a-z]+$/);
    assert.ok(group.label && group.detail, group.id);
  }
});

test('onboarding places a field in a feature group or on the Network page', () => {
  const places = new Set([...schema.FEATURE_GROUPS.map((group) => group.id), 'network']);
  for (const field of schema.FIELDS.filter((entry) => entry.onboarding)) {
    assert.ok(places.has(field.onboarding), `${field.path}: ${field.onboarding}`);
  }
});

test('the default path never asks for a device (onboarding rule 3.6)', () => {
  const devices = schema.FIELDS.filter((field) => /device/i.test(field.path) && field.onboarding);
  assert.deepEqual(devices.map((field) => field.path), []);
});

test('the proxy and the model source are on the Network page', () => {
  const network = schema.FIELDS.filter((field) => field.onboarding === 'network').map((field) => field.path);
  assert.deepEqual(network.sort(), ['httpProxy', 'httpsProxy', 'models.model_hub', 'noProxy']);
});

test('the old wizard flag is gone', () => {
  assert.deepEqual(schema.FIELDS.filter((field) => 'wizard' in field).map((field) => field.path), []);
});

test('describe() hands the groups and placements to the renderer', () => {
  const description = config.describe();
  assert.deepEqual(description.featureGroups, schema.FEATURE_GROUPS);
  const provider = description.fields.find((field) => field.path === 'models.asr.provider');
  assert.equal(provider.onboarding, 'recording');
  const device = description.fields.find((field) => field.path === 'models.asr.device');
  assert.equal(device.onboarding, null);
});

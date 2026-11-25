// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// While setup is working, the backend must not start: its interpreter exists
// seconds into creating the environment, but pip runs for minutes after.

const test = require('node:test');
const assert = require('node:assert/strict');

const { ServiceManager } = require('../../electron/services/process-manager.cjs');

const quietLogs = { setRoute() { }, setTap() { }, note() { }, logFile: () => null, clear() { }, openSink() { }, write() { } };
const SETUP = { code: 'setup', message: 'Setup is still preparing the Python environment.' };

function manager(guard) {
  const services = new ServiceManager(quietLogs);
  services.setStartGuard(guard);
  return services;
}

test('start is refused with the guard message', async () => {
  const services = manager(() => SETUP);
  // adopt: false, so nothing probes a port a real backend might be holding.
  await assert.rejects(services.startService('backend', { adopt: false }), { message: SETUP.message });
  assert.equal(services.snapshot().find((service) => service.id === 'backend').status, 'stopped');
});

test('restart is refused before it stops anything', async () => {
  const services = manager(() => SETUP);
  let stopped = false;
  services.stopService = async () => {
    stopped = true;
  };
  await assert.rejects(services.restartService('backend'), { message: SETUP.message });
  assert.equal(stopped, false);
});

test('the snapshot names the block for managed services only', () => {
  const blocked = manager(() => SETUP).snapshot();
  assert.equal(blocked.find((service) => service.id === 'backend').startBlockedBy, 'setup');
  for (const service of blocked.filter((entry) => !entry.managed)) assert.equal(service.startBlockedBy, null);

  const clear = manager(() => null).snapshot();
  assert.equal(clear.find((service) => service.id === 'backend').startBlockedBy, null);
});

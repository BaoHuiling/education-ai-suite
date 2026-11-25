// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The Setup screen's list of what is not working, and how a failure reads.

import { describe, expect, it } from 'vitest';
import { fixable, listedSteps, ownRepair, rowState, runError, willRun } from '../../src/services/problemList';
import { classifyFailure, isNetworkCause } from '../../src/services/setupFailure';
import type { SetupAction, SetupStep } from '../../src/types/setup';

const step = (id: string, patch: Partial<SetupStep> = {}): SetupStep =>
  ({
    id,
    label: id,
    section: 'software',
    optional: false,
    requires: [],
    actions: [],
    status: 'ok',
    detail: '',
    detailKey: null,
    detailParams: {},
    repair: null,
    repairKind: null,
    hint: null,
    ...patch,
  }) as SetupStep;

const action = (id: string, patch: Partial<SetupAction> = {}): SetupAction => ({
  id,
  label: id,
  destructive: false,
  elevates: false,
  ...patch,
});

describe('what Fix covers', () => {
  it('fixes what needs nobody and what an administrator approves, never what deletes', () => {
    expect(fixable(step('python', { status: 'missing', repairKind: 'auto' }))).toBe(true);
    expect(fixable(step('longPaths', { status: 'missing', repairKind: 'elevated' }))).toBe(true);
    expect(fixable(step('venv', { status: 'failed', repairKind: 'manual' }))).toBe(false);
    expect(fixable(step('gpu', { status: 'warn', repairKind: 'auto' }))).toBe(false);
  });

  it('gives a row its own button only for what Fix leaves alone', () => {
    const recreate = action('recreate', { destructive: true });
    const broken = step('venv', { status: 'failed', repair: 'recreate', repairKind: 'manual', actions: [recreate] });
    expect(ownRepair(broken)).toBe(recreate);
    const install = action('install');
    expect(ownRepair(step('python', { status: 'missing', repair: 'install', repairKind: 'auto', actions: [install] }))).toBeUndefined();
    // Nothing to offer: no repair, or nothing wrong.
    expect(ownRepair(step('npu', { status: 'missing' }))).toBeUndefined();
    expect(ownRepair(step('venv', { repair: 'recreate', actions: [recreate] }))).toBeUndefined();
  });
});

describe('the list', () => {
  it('lists what needs work, in setup order, and never warnings', () => {
    const steps = [step('gpu', { status: 'warn' }), step('python', { status: 'missing' }), step('venv', { status: 'outdated' }), step('ffmpeg')];
    expect(listedSteps(steps, [])).toEqual(['python', 'venv']);
  });

  it('keeps a listed step while it is checked or repaired again, and drops it once done', () => {
    const checking = [step('python', { status: 'running' }), step('venv', { status: 'unknown' })];
    expect(listedSteps(checking, ['python', 'venv'])).toEqual(['python', 'venv']);
    // A step being checked that was not a problem does not join.
    expect(listedSteps(checking, [])).toEqual([]);
    expect(listedSteps([step('python'), step('venv', { status: 'warn' })], ['python', 'venv'])).toEqual([]);
  });

  it('shows what a run fixed until the run is over', () => {
    const steps = [step('python'), step('venv', { status: 'running', detailKey: 'running' })];
    expect(listedSteps(steps, ['python', 'venv'], true)).toEqual(['python', 'venv']);
    expect(listedSteps(steps, ['python', 'venv'], false)).toEqual(['venv']);
  });

  it('knows what a run under way will still get to', () => {
    const python = step('python', { status: 'missing', repairKind: 'auto' });
    const longPaths = step('longPaths', { status: 'missing', repairKind: 'elevated' });
    // The app's own run at launch repairs only what needs nobody.
    expect(willRun(python, null)).toBe(true);
    expect(willRun(longPaths, null)).toBe(false);
    // Fix's run: its queue.
    expect(willRun(longPaths, ['longPaths'])).toBe(true);
    expect(willRun(python, ['longPaths'])).toBe(false);
    expect(willRun(step('python', { repairKind: 'auto' }), ['python'])).toBe(false);
  });

  it('reads where each listed step stands', () => {
    expect(rowState(step('a', { status: 'running', detailKey: 'running' }), true)).toBe('fixing');
    expect(rowState(step('a', { status: 'running' }), false)).toBe('checking');
    expect(rowState(step('a', { status: 'unknown' }), true)).toBe('checking');
    expect(rowState(step('a', { status: 'warn' }), false)).toBe('fixed');
    expect(rowState(step('a', { status: 'missing' }), true)).toBe('waiting');
    expect(rowState(step('a', { status: 'missing' }), false)).toBe('problem');
  });
});

describe('failures', () => {
  it('explains the error a run ended with, not what a check found', () => {
    const refused = { id: 'venv', error: 'Stop the backend first: it has files this step replaces in use.' };
    expect(runError(step('venv', { status: 'outdated', detailKey: 'venvStale' }), [refused])).toBe(refused.error);
    expect(runError(step('ffmpeg', { status: 'failed', detail: 'winget exited with code 1.' }), [])).toBe('winget exited with code 1.');
    expect(runError(step('venv', { status: 'failed', detailKey: 'venvBroken', detail: 'Its interpreter does not run' }), [])).toBeNull();
    expect(runError(step('python', { status: 'missing', detailKey: 'pythonNone' }), [])).toBeNull();
  });

  it('names the likely cause of a failed step', () => {
    expect(classifyFailure(["SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]'))"])).toBe('certificate');
    expect(classifyFailure(['Max retries exceeded (Caused by SSLError(... certificate verify failed ...))'])).toBe('certificate');
    expect(classifyFailure(["Max retries exceeded with url: /simple/openvino/ (Caused by ProxyError('Unable to connect to proxy'))"])).toBe('network');
    expect(classifyFailure(['OSError: [Errno 28] No space left on device'])).toBe('disk');
    expect(classifyFailure(['Stop the backend first: it has files this step replaces in use.'])).toBe('backendRunning');
    expect(classifyFailure(['PermissionError: [WinError 32] The process cannot access the file'])).toBe('filesInUse');
    expect(classifyFailure(['python.exe exited with code 1.'])).toBe('other');
    expect(classifyFailure(['Administrator approval was declined.'])).toBe('declined');
    // As huggingface_hub and the model hubs word them.
    expect(classifyFailure(['Got: ConnectError: [WinError 10061] No connection could be made because the target machine actively refused it'])).toBe('network');
    expect(classifyFailure(['Tunnel connection failed: 407 Proxy Authentication Required'])).toBe('network');
    expect(classifyFailure(['[E3002] [403] 操作过于频繁，请稍后再试!'])).toBe('rateLimit');
    expect(classifyFailure(['429 Client Error: Too Many Requests for url: https://huggingface.co/api/models/org/model'])).toBe('rateLimit');
  });

  it('can keep to the causes a screen can explain', () => {
    const lines = ["PermissionError: [WinError 5] Access is denied: 'C:\\models'", 'ConnectError: [Errno 11001] getaddrinfo failed'];
    expect(classifyFailure(lines)).toBe('filesInUse');
    expect(classifyFailure(lines, ['network', 'disk'])).toBe('network');
    expect(classifyFailure(['PermissionError: [WinError 5] Access is denied'], ['network'])).toBe('other');
  });

  it("reads the step's own error before the output around it", () => {
    // The output still has an earlier step's failure: a declined prompt.
    const output = ['=== DL Streamer failed: Administrator approval was declined. ===', 'Installing requirements.txt'];
    expect(classifyFailure(['Download failed', ...output])).toBe('network');
    expect(classifyFailure(['python.exe exited with code 1.', ...output, 'ProxyError: Unable to connect'])).toBe('network');
    // And that earlier step keeps its own cause, whatever the output says since.
    expect(classifyFailure(['Administrator approval was declined.', 'ProxyError: Unable to connect'])).toBe('declined');
  });

  it('sends network failures to the proxy settings', () => {
    expect(isNetworkCause('network')).toBe(true);
    expect(isNetworkCause('certificate')).toBe(true);
    expect(isNetworkCause('backendRunning')).toBe(false);
  });
});

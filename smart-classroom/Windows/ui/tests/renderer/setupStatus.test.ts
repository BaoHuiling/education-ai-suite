// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { blockedBy, iconOf, isActing, needsAdmin, needsWork } from '../../src/services/setupStatus';
import type { SetupStep } from '../../src/types/setup';

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

describe('needsWork', () => {
  it('counts missing, failed and outdated, not warnings', () => {
    expect(needsWork(step('a', { status: 'missing' }))).toBe(true);
    expect(needsWork(step('a', { status: 'outdated' }))).toBe(true);
    expect(needsWork(step('a', { status: 'warn' }))).toBe(false);
  });

  it('knows administrator tasks from the setup data, not from step ids', () => {
    expect(needsAdmin(step('anything', { status: 'missing', repairKind: 'elevated' }))).toBe(true);
    expect(needsAdmin(step('longPaths', { status: 'missing', repairKind: 'auto' }))).toBe(false);
    expect(needsAdmin(step('dlStreamer', { status: 'ok', repairKind: 'elevated' }))).toBe(false);
  });
});

describe('marks and actions', () => {
  it('maps statuses to marks', () => {
    expect(iconOf(step('a', { status: 'running' }))).toBe('running');
    expect(iconOf(step('a', { status: 'outdated' }))).toBe('warn');
    expect(iconOf(step('a', { status: 'missing' }))).toBe('failed');
    expect(iconOf(step('a', { status: 'unknown' }))).toBe('pending');
  });

  it('tells an action at work from a check', () => {
    expect(isActing(step('a', { status: 'running', detailKey: 'running' }))).toBe(true);
    expect(isActing(step('a', { status: 'running', detailKey: 'venvNone' }))).toBe(false);
  });

  it('names what a step is waiting for', () => {
    const steps = [
      step('python', { status: 'missing', label: 'Python 3.12' }),
      step('venv', { status: 'missing', requires: ['python'] }),
      step('grading', { requires: ['venv'], status: 'missing' }),
      step('layout', { requires: ['python'], status: 'missing' }),
    ];
    expect(blockedBy(steps)).toEqual({ venv: ['Python 3.12'], grading: ['venv'], layout: ['Python 3.12'] });
    // Outdated and warn satisfy a requirement.
    expect(blockedBy([step('python', { status: 'outdated' }), step('venv', { requires: ['python'] })])).toEqual({});
  });
});

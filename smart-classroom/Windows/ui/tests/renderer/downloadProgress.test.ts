// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { formatBytes, fractionDone, hubName, runningDownload, timeLeft } from '../../src/services/downloadProgress';
import type { DownloadProgress } from '../../src/types/download';

const MB = 1024 * 1024;
const GB = 1024 * MB;

const progress = (patch: Partial<DownloadProgress> = {}): DownloadProgress => ({
  repo: 'org/model',
  hub: 'huggingface',
  state: 'running',
  done: 0,
  total: 0,
  speed: null,
  files: 0,
  fileTotal: null,
  ...patch,
});

describe('formatBytes', () => {
  it('uses the units File Explorer does, to three significant digits', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1536)).toBe('1.50 KB');
    expect(formatBytes(3.421 * MB)).toBe('3.42 MB');
    expect(formatBytes(34.21 * MB)).toBe('34.2 MB');
    expect(formatBytes(342.1 * MB)).toBe('342 MB');
    expect(formatBytes(12.5 * GB)).toBe('12.5 GB');
  });

  it('moves up a unit where rounding reaches 1024', () => {
    expect(formatBytes(1023)).toBe('1023 B');
    expect(formatBytes(1023.7 * 1024)).toBe('1.00 MB');
  });

  it('shows nonsense as nothing', () => {
    expect(formatBytes(-5)).toBe('0 B');
    expect(formatBytes(Number.NaN)).toBe('0 B');
  });
});

describe('fractionDone', () => {
  it('is unknown until the first file starts', () => {
    expect(fractionDone(progress())).toBeNull();
  });

  it('measures against the bytes known so far', () => {
    expect(fractionDone(progress({ done: 25, total: 100 }))).toBe(0.25);
    expect(fractionDone(progress({ done: 120, total: 100 }))).toBe(1);
  });
});

describe('timeLeft', () => {
  it('needs a speed and a size', () => {
    expect(timeLeft(progress({ done: 10, total: 100 }))).toBeNull();
    expect(timeLeft(progress({ speed: 5 }))).toBeNull();
  });

  it('rounds up to the minute, then speaks in hours', () => {
    expect(timeLeft(progress({ done: 0, total: 50 * MB, speed: MB }))).toEqual({ unit: 'seconds' });
    expect(timeLeft(progress({ done: 0, total: 61 * MB, speed: MB }))).toEqual({ unit: 'minutes', minutes: 2 });
    expect(timeLeft(progress({ done: 0, total: 3600 * MB, speed: MB }))).toEqual({ unit: 'hours', hours: 1, minutes: 0 });
    expect(timeLeft(progress({ done: 600 * MB, total: 5000 * MB, speed: MB }))).toEqual({
      unit: 'hours',
      hours: 1,
      minutes: 14,
    });
  });
});

describe('runningDownload', () => {
  it('passes on a download under way only', () => {
    const running = progress();
    expect(runningDownload(running)).toBe(running);
    expect(runningDownload(progress({ state: 'done' }))).toBeNull();
    expect(runningDownload(progress({ state: 'failed' }))).toBeNull();
    expect(runningDownload(null)).toBeNull();
    expect(runningDownload(undefined)).toBeNull();
  });
});

describe('hubName', () => {
  it('names the hubs as people know them', () => {
    expect(hubName('huggingface')).toBe('Hugging Face');
    expect(hubName('modelscope')).toBe('ModelScope');
    expect(hubName('mirror')).toBe('mirror');
  });
});

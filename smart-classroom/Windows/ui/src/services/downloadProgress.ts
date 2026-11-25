// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The numbers behind a model download's progress bar (components/ui/
// DownloadBar): sizes, how far, and how long is left.

import type { DownloadProgress } from '../types/download';

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

/** "512 B", "3.42 MB", "12.5 GB": 1024-based, as File Explorer shows sizes. */
export function formatBytes(bytes: number): string {
  let value = Number.isFinite(bytes) ? Math.max(0, bytes) : 0;
  let unit = 0;
  for (;;) {
    // Three significant digits: 3.42 MB, 34.2 MB, 342 MB.
    const digits = unit === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2;
    const text = value.toFixed(digits);
    // 1023.7 KB rounds to 1024 KB, which is 1.00 MB.
    if (Number(text) >= 1024 && unit < UNITS.length - 1) {
      value /= 1024;
      unit += 1;
      continue;
    }
    return `${text} ${UNITS[unit]}`;
  }
}

/** A running download, or null for one that ended or never started. */
export const runningDownload = (progress: DownloadProgress | null | undefined): DownloadProgress | null =>
  progress?.state === 'running' ? progress : null;

/**
 * How far, from 0 to 1; null before the first file starts, when there is no
 * size to measure against.
 */
export function fractionDone(progress: DownloadProgress): number | null {
  if (!progress.total) return null;
  return Math.min(1, progress.done / progress.total);
}

export type TimeLeft =
  | { unit: 'seconds' }
  | { unit: 'minutes'; minutes: number }
  | { unit: 'hours'; hours: number; minutes: number };

/**
 * About how long is left at the current speed, or null when there is no
 * telling: no speed yet, or nothing to measure against.
 */
export function timeLeft(progress: DownloadProgress): TimeLeft | null {
  if (!progress.speed || !progress.total) return null;
  const seconds = Math.max(0, progress.total - progress.done) / progress.speed;
  if (seconds < 60) return { unit: 'seconds' };
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return { unit: 'minutes', minutes };
  return { unit: 'hours', hours: Math.floor(minutes / 60), minutes: minutes % 60 };
}

/** The hub as people know it. */
export const hubName = (hub: string): string =>
  hub === 'modelscope' ? 'ModelScope' : hub === 'huggingface' ? 'Hugging Face' : hub;

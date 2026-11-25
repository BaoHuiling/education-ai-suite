// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react';

/** The current time, refreshed every `interval` ms while `active`. */
export function useNow(active: boolean, interval = 500): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), interval);
    return () => clearInterval(timer);
  }, [active, interval]);
  return now;
}

/** "m:ss", or "h:mm:ss" past an hour. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

/** When each step of a run started, for the elapsed time beside the one running. */
export function useStartTimes(runningId: string | null): Record<string, number> {
  const [startedAt, setStartedAt] = useState<Record<string, number>>({});
  useEffect(() => {
    if (runningId && !startedAt[runningId]) setStartedAt((previous) => ({ ...previous, [runningId]: Date.now() }));
  }, [runningId, startedAt]);
  return startedAt;
}

// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Typed bridge to the first-run wizard's flow, which the main process owns
// (electron/services/first-run.cjs). The window shows it and sends choices.

import { useCallback, useEffect, useRef, useState } from 'react';
import type { FirstRunChoice, FirstRunSnapshot, ProxyValues } from '../types/firstRun';
import { unwrap } from './ipcResult';

export const isFirstRunAvailable = (): boolean => !!window.electronAPI?.firstRun;

export const sendFirstRunChoice = (choice: FirstRunChoice, options?: { includeElevated?: boolean }) =>
  unwrap(window.electronAPI?.firstRun?.send(choice, options));

/** The Network page's test of typed proxy values. */
export const probeNetwork = (values: ProxyValues) => unwrap(window.electronAPI?.network?.probe(values));

/** The flow's state, pushed by the main process, and a way to send choices. */
export function useFirstRun() {
  const [snapshot, setSnapshot] = useState<FirstRunSnapshot | null>(null);
  // A push can overtake the first read; the push is the newer state.
  const pushed = useRef(false);

  useEffect(() => {
    if (!isFirstRunAvailable()) return;
    let cancelled = false;

    unwrap(window.electronAPI?.firstRun?.get())
      .then((initial) => {
        if (!cancelled && !pushed.current) setSnapshot(initial);
      })
      .catch(() => undefined);

    const unsubscribe = window.electronAPI?.firstRun?.onChanged((next) => {
      if (cancelled) return;
      pushed.current = true;
      setSnapshot(next);
    });

    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, []);

  const send = useCallback(async (choice: FirstRunChoice, options?: { includeElevated?: boolean }) => {
    setSnapshot(await sendFirstRunChoice(choice, options));
  }, []);

  return { snapshot, send };
}

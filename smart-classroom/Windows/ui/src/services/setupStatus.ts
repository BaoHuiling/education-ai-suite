// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// What a setup step's status means, in one place. The first run, the Setup
// screen and the app's landing logic all ask these questions; before this
// module each answered them its own way.

import type { StatusState } from '../components/ui';
import type { SetupStep } from '../types/setup';

/** Statuses that stand between the machine and being ready to run. */
export const NEEDS_WORK: SetupStep['status'][] = ['missing', 'failed', 'outdated'];

/** Statuses a step's `requires` can be in for it to run: neither warn nor outdated blocks. */
export const SATISFIED: SetupStep['status'][] = ['ok', 'warn', 'outdated'];

export const needsWork = (step: SetupStep) => NEEDS_WORK.includes(step.status);
export const isRunning = (step: SetupStep) => step.status === 'running';
export const isDone = (step: SetupStep) => step.status === 'ok' || step.status === 'warn';

/**
 * A setup action at work (an install, pip), as opposed to a check: runStep
 * gives the row the 'running' detail, a check keeps the row's previous one.
 */
export const isActing = (step: SetupStep) => step.status === 'running' && step.detailKey === 'running';

/** Needs work that only administrator approval can do. */
export const needsAdmin = (step: SetupStep) => needsWork(step) && step.repairKind === 'elevated';

/** The mark a step shows. */
export function iconOf(step: SetupStep): StatusState {
  switch (step.status) {
    case 'running':
      return 'running';
    case 'ok':
      return 'ok';
    case 'warn':
    case 'outdated':
      return 'warn';
    case 'missing':
    case 'failed':
      return 'failed';
    default:
      return 'pending';
  }
}

/**
 * Steps that cannot run yet because something they need is not satisfied,
 * mapped to the labels of what they need. Without this an action is live and
 * fails with "Create the Python environment first" only after the click.
 */
export function blockedBy(steps: SetupStep[]): Record<string, string[]> {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const map: Record<string, string[]> = {};
  for (const step of steps) {
    const unmet = step.requires
      .map((id) => byId.get(id))
      .filter((required): required is SetupStep => !!required && !SATISFIED.includes(required.status))
      .map((required) => required.label);
    if (unmet.length) map[step.id] = unmet;
  }
  return map;
}

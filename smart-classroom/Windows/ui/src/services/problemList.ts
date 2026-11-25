// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The Setup screen's list of what is not working: only the problems, one row
// each, and what clears each one. Pure, so tests/renderer covers it.

import type { AutomaticRunResult, SetupAction, SetupStep } from '../types/setup';
import { isActing, isDone, needsWork } from './setupStatus';

/**
 * Cleared by the Fix button: the repairs that need nobody, and those Windows
 * asks an administrator to approve. Fix is the first launch's automatic run
 * with the administrator tasks included.
 */
export const fixable = (step: SetupStep) =>
  needsWork(step) && (step.repairKind === 'auto' || step.repairKind === 'elevated');

/**
 * The repair a row offers itself because Fix leaves it alone: a destructive
 * one, such as Recreate, which asks before it deletes anything.
 */
export function ownRepair(step: SetupStep): SetupAction | undefined {
  if (!needsWork(step) || fixable(step) || !step.repair) return undefined;
  return step.actions.find((action) => action.id === step.repair);
}

/**
 * The steps the list shows, in setup order: those that need work. A listed one
 * stays while it is checked or repaired again, when its status reads 'running'
 * or 'unknown' for a while, and leaves once it reads done; otherwise every
 * Re-check would empty the list and claim that all is well. With `holdDone`,
 * while a run is under way, a listed one stays even when done, so the run
 * shows what it fixed until it ends.
 */
export function listedSteps(steps: SetupStep[], previous: string[], holdDone = false): string[] {
  return steps
    .filter((step) => needsWork(step) || (previous.includes(step.id) && (holdDone || !isDone(step))))
    .map((step) => step.id);
}

/**
 * Whether an automatic run under way will still get to this step: one in the
 * queue of the Fix run this screen started, or, for the run the app starts by
 * itself at launch, one it repairs unattended.
 */
export function willRun(step: SetupStep, queue: string[] | null): boolean {
  if (!needsWork(step)) return false;
  return queue ? queue.includes(step.id) : step.repairKind === 'auto';
}

export type RowState = 'problem' | 'checking' | 'waiting' | 'fixing' | 'fixed';

/** Where a listed step stands. `waiting`: willRun() during an automatic run. */
export function rowState(step: SetupStep, waiting: boolean): RowState {
  if (isActing(step)) return 'fixing';
  if (step.status === 'running' || step.status === 'unknown') return 'checking';
  if (isDone(step)) return 'fixed';
  return waiting ? 'waiting' : 'problem';
}

/**
 * The error a listed step's last run ended with: from the Fix run's result,
 * which also has the refusals that never reach the step (such as "stop the
 * backend first"), or as a run elsewhere recorded it on the step. A check's
 * finding is not an error to explain; it comes with a detail key.
 */
export function runError(step: SetupStep, failed: AutomaticRunResult['failed']): string | null {
  const reported = failed.find((entry) => entry.id === step.id);
  if (reported) return reported.error;
  return step.status === 'failed' && !step.detailKey ? step.detail : null;
}

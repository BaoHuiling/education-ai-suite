// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The setup assistant's decisions that need no React: which page shows, and
// what the step rail and the Install list say. Pure, so tests/renderer covers
// them. The flow itself is the main process's (electron/services/first-run.cjs).

import type { FirstRunSnapshot, ModelHub, SetupPlanEntry } from '../../types/firstRun';
import type { SetupStep } from '../../types/setup';
import { classifyFailure, type FailureCause } from '../../services/setupFailure';
import { isDone, needsAdmin } from '../../services/setupStatus';

export type AssistantPage = 'welcome' | 'network' | 'features' | 'admin' | 'install' | 'done';

export type ActiveSnapshot = Extract<FirstRunSnapshot, { active: true }>;

/** The pages in order. Administrator tasks appear only when there are some. */
export function pageOrder(withAdmin: boolean): AssistantPage[] {
  return withAdmin
    ? ['welcome', 'network', 'features', 'admin', 'install', 'done']
    : ['welcome', 'network', 'features', 'install', 'done'];
}

/** Pages the user can move between while customizing the first run. */
const CUSTOMIZE_PAGES: AssistantPage[] = ['welcome', 'network', 'features', 'admin'];

/**
 * The page that shows. The main process decides the phase; `chosen` is where
 * the user went within the pages that phase allows, or null for its default.
 */
export function onboardingPage(snapshot: FirstRunSnapshot | null, chosen: AssistantPage | null): AssistantPage | null {
  if (!snapshot?.active) return null;
  const customizing = chosen && CUSTOMIZE_PAGES.includes(chosen) ? chosen : 'network';
  switch (snapshot.phase) {
    case 'checking':
      // Customize during the checks: the pages are usable while they finish.
      return snapshot.held === 'customize' ? customizing : 'welcome';
    case 'countdown':
      return 'welcome';
    case 'waiting':
      // A failed network test stops the flow on the thing to fix; from there
      // the pages work as when customizing.
      if (snapshot.held === 'customize' || snapshot.held === 'network') return customizing;
      if (snapshot.held === 'cancelled') return chosen === 'network' ? 'network' : 'install';
      return 'welcome';
    case 'installing':
      return 'install';
    case 'finished':
      // Network: from Done, when the backend's first start failed on a download.
      if (snapshot.result?.envOk) return chosen === 'network' ? 'network' : 'done';
      return chosen === 'network' ? 'network' : 'install';
    default:
      return null;
  }
}

export type RailState = 'complete' | 'current' | 'upcoming';

/** The step rail: progress, so the pages before the current one are complete. */
export function railItems(order: AssistantPage[], page: AssistantPage): { id: AssistantPage; state: RailState }[] {
  const index = order.indexOf(page);
  return order.map((id, position) => ({
    id,
    state: position === index ? 'current' : position < index ? 'complete' : 'upcoming',
  }));
}

/** Whole seconds left of a countdown, never negative. */
export function secondsLeft(endsAt: number | null, now: number): number {
  if (endsAt === null) return 0;
  return Math.max(0, Math.ceil((endsAt - now) / 1000));
}

// ---------------------------------------------------------------------------
// Install page
// ---------------------------------------------------------------------------

export type ItemStatus = 'pending' | 'running' | 'done' | 'failed';

/** Administrator tasks this machine still needs, as the setup data flags them. */
export const adminTasks = (steps: SetupStep[]) => steps.filter(needsAdmin);

/**
 * The steps an installation runs, in its order: the queue captured when it
 * started, minus elevated steps nobody approved (they wait on the approval
 * card) and anything destructive.
 */
export function installSteps(plan: SetupPlanEntry[] | null, steps: SetupStep[], includeElevated: boolean): SetupStep[] {
  return (plan ?? [])
    .filter((entry) => entry.kind === 'auto' || (entry.kind === 'elevated' && includeElevated))
    .map((entry) => steps.find((step) => step.id === entry.stepId))
    .filter((step): step is SetupStep => !!step);
}

/** Where a step of an installation is. After the run, one still not done did not get done. */
export function itemStatus(step: SetupStep, runOver: boolean): ItemStatus {
  if (step.status === 'running') return 'running';
  if (isDone(step)) return 'done';
  if (step.status === 'failed') return 'failed';
  return runOver ? 'failed' : 'pending';
}

/** Elevated steps of the installation still waiting for someone to approve them. */
export function awaitingApproval(snapshot: ActiveSnapshot, steps: SetupStep[]): SetupStep[] {
  if (snapshot.includeElevated) return [];
  const ids = (snapshot.plan ?? []).filter((entry) => entry.kind === 'elevated').map((entry) => entry.stepId);
  return steps.filter((step) => ids.includes(step.id) && !isDone(step));
}

/** "Step n of m": the running item, else the first not yet done. */
export function stepCounter(statuses: ItemStatus[], extra = 0): { step: number; total: number } {
  const total = statuses.length + extra;
  const running = statuses.indexOf('running');
  if (running >= 0) return { step: running + 1, total };
  const done = statuses.filter((status) => status === 'done').length;
  return { step: Math.min(done + 1, total), total };
}

/** The model the backend is downloading, from the line download_snapshot() logs. */
export function modelDownload(lines: string[]): { repo: string; hub: ModelHub } | null {
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const match = /Downloading (\S+?)@\S+ from (Hugging Face|ModelScope)/.exec(lines[index]);
    if (match) return { repo: match[1], hub: match[2] === 'ModelScope' ? 'modelscope' : 'huggingface' };
  }
  return null;
}

// The causes of a failed first start the Done page can say something useful
// about: those of the model downloads it makes. Files in use and the like
// read the same in its log for other reasons, so they stay "other".
const START_CAUSES: FailureCause[] = ['certificate', 'disk', 'rateLimit', 'network'];

// The line a Python exception ends on: "utils.ensure_model.ModelDownloadError: ...".
const EXCEPTION_LINE = /^\s*(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception): \S/;

/**
 * Why the backend's first start failed, read from its log: the error it ended
 * on, and what that most likely means. The models download during the first
 * start, so a network failure is the usual cause.
 */
export function startFailure(lines: string[]): { cause: FailureCause; error: string | null } {
  const tail = lines.slice(-200);
  // The last one: a chained exception ends on the one that wraps the others.
  const error = [...tail].reverse().find((line) => EXCEPTION_LINE.test(line))?.trim() ?? null;
  return { cause: classifyFailure([error ?? '', ...tail], START_CAUSES), error };
}

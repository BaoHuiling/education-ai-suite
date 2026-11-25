// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Shapes returned by the Electron setup IPC bridge. Kept in sync with
// electron/services/setup-runner.cjs.

import type { DownloadProgress } from './download';

export type SetupStatus = 'unknown' | 'ok' | 'warn' | 'outdated' | 'missing' | 'running' | 'failed';

export interface SetupAction {
  id: string;
  label: string;
  /** Destroys existing state, so it is confirmed natively and never bulk-run. */
  destructive: boolean;
  /** Raises a UAC prompt, so it never runs without someone approving it. */
  elevates: boolean;
}

export interface SetupSection {
  id: string;
  label: string;
}

export interface SetupStep {
  id: string;
  label: string;
  section: string;
  optional: boolean;
  /** Ids of steps that must be OK before this one's actions can work. */
  requires: string[];
  actions: SetupAction[];
  status: SetupStatus;
  /** English; the fallback when there is no `detailKey` or no translation for it. */
  detail: string;
  /** setup.details.<key> in src/i18n. Null for raw error text. */
  detailKey: string | null;
  detailParams: Record<string, string | number>;
  /** The action that clears this status. */
  repair: string | null;
  /**
   * How that repair runs: 'auto' needs nobody, 'elevated' needs administrator
   * approval, 'manual' (destructive) is left for a deliberate click.
   */
  repairKind: 'auto' | 'elevated' | 'manual' | null;
  /** A command or link the user can use when the app cannot fix it. */
  hint: string | null;
  /** While an action of this step runs: the model download it last reported. */
  progress?: DownloadProgress | null;
}

/**
 * Codes a failed setup action can carry, for failures the screen can offer a
 * way out of. Mirrors SETUP_ERROR in electron/services/setup-runner.cjs.
 */
export const SETUP_ERROR = {
  /** The Python environment cannot be touched while the backend is using it. */
  BACKEND_RUNNING: 'backend-running',
} as const;

export interface SetupDescription {
  sections: SetupSection[];
  steps: SetupStep[];
}

/** What SetupRunner.runAutomatic() did. */
export interface AutomaticRunResult {
  ran: string[];
  failed: { id: string; error: string }[];
  blocked: string[];
  manual: string[];
}

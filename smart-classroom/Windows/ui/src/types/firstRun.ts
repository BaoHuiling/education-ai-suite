// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The first-run wizard's flow as the main process reports it. Mirrors
// snapshot() in electron/services/first-run.cjs and the results of
// electron/services/network-probe.cjs; keep them in step.

export type FirstRunPhase = 'checking' | 'countdown' | 'waiting' | 'installing' | 'finished' | 'manual';

/** Why the flow waits instead of counting down. */
export type FirstRunHold = 'choice' | 'customize' | 'network' | 'disk' | 'setup' | 'cancelled';

/** The only choices the window can send. */
export type FirstRunChoice = 'customize' | 'installNow' | 'approveElevated' | 'manual';

export type NetworkCause =
  | 'dns'
  | 'timeout'
  | 'connection'
  | 'proxyUnreachable'
  | 'proxyAuth'
  | 'proxyRefused'
  | 'certificate'
  | 'unsupportedProxy'
  | 'other';

export type ModelHub = 'huggingface' | 'modelscope';

export type HostGroup = 'packages' | 'huggingface' | 'modelscope' | 'installers';

export interface HostResult {
  host: string;
  group: HostGroup;
  ok: boolean;
  cause: NetworkCause | null;
  /** The error code or HTTP status behind the cause. */
  detail: string;
  ms: number;
}

export interface ProxyCandidate {
  source: 'saved' | 'environment' | 'system' | 'direct' | 'entered';
  /** Credentials are replaced with `***`. */
  httpProxy: string;
  httpsProxy: string;
  noProxy: string;
}

export interface TriedCandidate extends ProxyCandidate {
  results: HostResult[];
}

export interface NetworkEvaluation {
  /** The recommended settings will work, once suggestedHub is applied. */
  ok: boolean;
  packages: boolean;
  installers: boolean;
  needInstallers: boolean;
  hubs: Record<ModelHub, boolean>;
  hub: ModelHub;
  suggestedHub: ModelHub | null;
}

export interface FirstRunNetwork extends NetworkEvaluation {
  chosen: TriedCandidate | null;
  tried: TriedCandidate[];
}

export interface FirstRunResult {
  ran: string[];
  failed: { id: string | null; error: string }[];
  blocked: string[];
  manual: string[];
  /** The Python environment is OK, so the backend can start. */
  envOk: boolean;
}

export interface FirstRunRecordView {
  stage: 'welcome' | 'customize' | 'installing' | 'done';
  mode: 'recommended' | 'custom' | 'manual';
  completedAt: string | null;
}

export type FirstRunSnapshot =
  | { active: false; record: FirstRunRecordView | null }
  | {
      active: true;
      phase: FirstRunPhase;
      held: FirstRunHold | null;
      /** Epoch milliseconds when the countdown ends; null outside it. */
      endsAt: number | null;
      diskLow: boolean;
      recommended: boolean;
      includeElevated: boolean;
      network: FirstRunNetwork | null;
      /** The queue as it stood when the installation started; null before. */
      plan: SetupPlanEntry[] | null;
      result: FirstRunResult | null;
      record: FirstRunRecordView | null;
    };

export interface SetupPlanEntry {
  stepId: string;
  actionId: string;
  kind: 'auto' | 'elevated' | 'manual';
}

export interface ProxyValues {
  httpProxy: string;
  httpsProxy: string;
  noProxy: string;
}

export interface NetworkProbeResult {
  candidate: ProxyCandidate;
  results: HostResult[];
  evaluation: NetworkEvaluation;
}

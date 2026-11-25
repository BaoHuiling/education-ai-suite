// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';

export type StatusState = 'ok' | 'warn' | 'failed' | 'running' | 'pending';

const GLYPH: Record<Exclude<StatusState, 'running'>, string> = { ok: '✓', warn: '!', failed: '✕', pending: '' };

/** Status as a mark: done, warning, failed, in progress or not started. */
const StatusIcon: React.FC<{ state: StatusState; label?: string }> = ({ state, label }) => {
  if (state === 'running') return <span className="ui-spinner" role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
  return (
    <span className={`ui-status ui-status-${state}`} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      {GLYPH[state]}
    </span>
  );
};

export default StatusIcon;

// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import StatusIcon, { type StatusState } from './StatusIcon';

interface RowProps {
  state: StatusState;
  title: React.ReactNode;
  /** A small tag beside the title, such as "optional". */
  badge?: React.ReactNode;
  /** Right-aligned: a status word, a time. */
  meta?: React.ReactNode;
  detail?: React.ReactNode;
  actions?: React.ReactNode;
  flashed?: boolean;
  selected?: boolean;
  onSelect?: () => void;
  rowRef?: (node: HTMLLIElement | null) => void;
  id?: string;
}

/** One line of a status list: the checklist, setup steps, services, hosts. */
export const Row: React.FC<RowProps> = ({
  state,
  title,
  badge,
  meta,
  detail,
  actions,
  flashed,
  selected,
  onSelect,
  rowRef,
  id,
}) => (
  <li
    id={id}
    ref={rowRef}
    className={['ui-row', state, flashed && 'flashed', onSelect && 'selectable', selected && 'selected']
      .filter(Boolean)
      .join(' ')}
    onClick={onSelect}
    aria-selected={onSelect ? !!selected : undefined}
  >
    <span className="ui-row-icon">
      <StatusIcon state={state} />
    </span>
    <span className="ui-row-head">
      <span className="ui-row-title">{title}</span>
      {badge && <span className="ui-row-badge">{badge}</span>}
    </span>
    <span className="ui-row-meta">{meta}</span>
    {detail && <div className="ui-row-detail">{detail}</div>}
    {actions && <div className="ui-row-actions">{actions}</div>}
  </li>
);

/** A bordered list of rows, with an optional heading. */
export const RowList: React.FC<{ title?: React.ReactNode; children: React.ReactNode; label?: string }> = ({
  title,
  children,
  label,
}) => (
  <div className="ui-rows">
    {title && <h4 className="ui-rows-title">{title}</h4>}
    <ul className="ui-rows-list" aria-label={label}>
      {children}
    </ul>
  </div>
);

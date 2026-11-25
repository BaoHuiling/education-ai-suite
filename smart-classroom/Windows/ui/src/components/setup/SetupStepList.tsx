// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Setup steps as status rows, the same on every screen: grouped by section
// (the Setup screen's checks) or not (the first run's lists), with or without
// their action buttons.

import React from 'react';
import { useTranslation } from 'react-i18next';
import type { SetupSection, SetupStep } from '../../types/setup';
import { blockedBy, iconOf } from '../../services/setupStatus';
import { Button, Row, RowList, type StatusState } from '../ui';

const STATUS_LABELS: Record<string, string> = {
  unknown: 'setup.status.unknown',
  ok: 'setup.status.ok',
  warn: 'setup.status.warn',
  outdated: 'setup.status.outdated',
  missing: 'setup.status.missing',
  running: 'setup.status.running',
  failed: 'setup.status.failed',
};

const isLink = (hint: string) => /^https?:\/\//.test(hint);

interface SetupStepListProps {
  steps: SetupStep[];
  groupBy?: 'section' | 'none';
  /** Section names, for groupBy 'section'. */
  sections?: SetupSection[];
  /** Commands as well as links ('all'), links only, or neither. */
  hints?: 'all' | 'links' | 'none';
  /** Each step's action buttons. Without this the list is read-only. */
  actions?: {
    disabled: boolean;
    /** A step whose action is running shows no buttons. */
    hiddenFor?: string | null;
    onRun: (stepId: string, actionId: string) => void;
  };
  /** Right-aligned text; the status word when not given. */
  meta?: (step: SetupStep) => React.ReactNode;
  /** More detail under a step, such as the line it last printed. */
  more?: (step: SetupStep) => React.ReactNode;
  /** The mark, when the step's own status is not the whole story. */
  stateOf?: (step: SetupStep) => StatusState;
  /** Extra rows at the end of an ungrouped list. */
  children?: React.ReactNode;
}

const SetupStepList: React.FC<SetupStepListProps> = ({
  steps,
  groupBy = 'none',
  sections = [],
  hints = 'all',
  actions,
  meta,
  more,
  stateOf,
  children,
}) => {
  const { t } = useTranslation();
  const blocked = blockedBy(steps);

  const hintOf = (step: SetupStep) => {
    if (!step.hint || hints === 'none') return null;
    if (isLink(step.hint)) {
      // target=_blank reaches the main process window-open handler, which
      // sends it to the system browser instead of the app window.
      return (
        <a className="ui-mono" href={step.hint} target="_blank" rel="noreferrer">
          {step.hint}
        </a>
      );
    }
    return hints === 'all' ? <code className="ui-hint-code">{step.hint}</code> : null;
  };

  const row = (step: SetupStep) => (
    <Row
      key={step.id}
      state={stateOf?.(step) ?? iconOf(step)}
      title={step.label}
      badge={step.optional ? t('setup.optional', 'optional') : undefined}
      meta={meta ? meta(step) : t(STATUS_LABELS[step.status] ?? step.status, step.status)}
      detail={
        <>
          {step.detail && <span>{step.detail}</span>}
          {hintOf(step)}
          {blocked[step.id] && (
            <span className="ui-row-blocked">
              {t('setup.blockedBy', 'Needs {{items}} first.', { items: blocked[step.id].join(', ') })}
            </span>
          )}
          {more?.(step)}
        </>
      }
      actions={
        actions && step.actions.length > 0 && actions.hiddenFor !== step.id
          ? step.actions.map((action) => (
              <Button
                key={action.id}
                variant={action.destructive ? 'danger' : 'secondary'}
                disabled={actions.disabled || step.status === 'unknown' || !!blocked[step.id]}
                onClick={() => actions.onRun(step.id, action.id)}
              >
                {action.label}
              </Button>
            ))
          : undefined
      }
    />
  );

  if (groupBy === 'section') {
    return (
      <>
        {sections.map((section) => {
          const members = steps.filter((step) => step.section === section.id);
          if (!members.length) return null;
          return (
            <RowList key={section.id} title={t(`setup.sections.${section.id}`, section.label)}>
              {members.map(row)}
            </RowList>
          );
        })}
      </>
    );
  }

  return (
    <RowList>
      {steps.map(row)}
      {children}
    </RowList>
  );
};

export default SetupStepList;

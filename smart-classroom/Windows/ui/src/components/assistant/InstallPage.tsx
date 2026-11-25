// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The unattended part of the first run: what runs, how far it is, and — when
// it stops — why and what to do next.

import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { SetupStep } from '../../types/setup';
import { cancelSetup, copyText, revealSetupLogs, useSetupLogs } from '../../services/setupManager';
import { classifyFailure, isNetworkCause, type FailureCause } from '../../services/setupFailure';
import { runningDownload } from '../../services/downloadProgress';
import { formatElapsed, useNow, useStartTimes } from '../../hooks/useNow';
import SetupStepList from '../setup/SetupStepList';
import { useFailureText } from '../setup/failureText';
import { Banner, Button, DownloadBar, LogViewer, PageHeader, Row, type StatusState } from '../ui';
import { awaitingApproval, installSteps, itemStatus, stepCounter, type ActiveSnapshot, type ItemStatus } from './assistantModel';

const STATE: Record<ItemStatus, StatusState> = { pending: 'pending', running: 'running', done: 'ok', failed: 'failed' };

interface InstallPageProps {
  snapshot: ActiveSnapshot;
  steps: SetupStep[];
  onApprove: () => void;
  onRetry: () => void;
  onNetwork: () => void;
  onManual: () => void;
  onOpenSetup: () => void;
}

const InstallPage: React.FC<InstallPageProps> = ({ snapshot, steps, onApprove, onRetry, onNetwork, onManual, onOpenSetup }) => {
  const { t } = useTranslation();
  const failureText = useFailureText();
  const { lines, clear } = useSetupLogs();
  const [showDetails, setShowDetails] = useState(false);
  const [copied, setCopied] = useState(false);

  const installing = snapshot.phase === 'installing';
  const cancelled = snapshot.phase === 'waiting' && snapshot.held === 'cancelled';
  const failed = snapshot.phase === 'finished' && !snapshot.result?.envOk;

  // Disabled once clicked, until the installation stops: an administrator
  // task under way runs to its end first, which can take minutes.
  const [cancelling, setCancelling] = useState(false);
  if (cancelling && !installing) setCancelling(false);
  const requestCancel = () => {
    setCancelling(true);
    cancelSetup().catch(() => setCancelling(false));
  };

  const queue = useMemo(() => installSteps(snapshot.plan, steps, snapshot.includeElevated), [snapshot, steps]);
  const statusOf = (step: SetupStep) => itemStatus(step, snapshot.phase === 'finished');
  const approvals = useMemo(() => awaitingApproval(snapshot, steps), [snapshot, steps]);
  // The backend's first start is the last item; it runs once setup is done.
  const counter = stepCounter(queue.map(statusOf), 1);

  // When each step started, for its elapsed time.
  const runningId = queue.find((step) => statusOf(step) === 'running')?.id ?? null;
  const startedAt = useStartTimes(runningId);
  const now = useNow(installing, 1000);
  const lastLine = lines.length ? lines[lines.length - 1].text : '';

  const failure = snapshot.result?.failed?.[0];
  const failedStep = steps.find((step) => step.id === (failure?.id ?? queue.find((entry) => statusOf(entry) === 'failed')?.id));
  const errorText = failure?.error ?? failedStep?.detail ?? '';
  const cause: FailureCause = classifyFailure([errorText, ...lines.slice(-80).map((line) => line.text)]);

  const copyDiagnostics = async () => {
    const report = [
      `Smart Classroom first-run installation — ${new Date().toISOString()}`,
      `phase=${snapshot.phase} held=${snapshot.held ?? '-'} cause=${cause}`,
      '',
      ...steps.map((step) => `${step.status.padEnd(8)} ${step.label}: ${step.detail}`),
      '',
      ...lines.slice(-200).map((line) => line.text),
    ].join('\n');
    await copyText(report).catch(() => undefined);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };

  return (
    <div className="assistant-page">
      <PageHeader
        focusOnMount
        title={
          failed
            ? t('assistant.install.failedTitle', 'Setup could not finish')
            : cancelled
              ? t('assistant.install.stoppedTitle', 'Setup stopped')
              : t('assistant.install.title', 'Installing')
        }
        subtitle={installing ? t('assistant.install.counter', 'Step {{step}} of {{total}}', counter) : undefined}
      />

      {failed && (
        <Banner tone="error">
          <p>
            <strong>{failedStep?.label ?? t('assistant.install.aStep', 'A step')}</strong>: {failureText(cause)}
          </p>
          {errorText && <p className="ui-mono">{errorText}</p>}
        </Banner>
      )}

      {cancelled && (
        <Banner tone="warning">
          {t('assistant.install.stopped', 'Nothing is lost: what finished stays installed, and Resume picks up the rest.')}
        </Banner>
      )}

      {approvals.length > 0 && !failed && (
        <Banner actions={<Button onClick={onApprove}>{t('assistant.install.approve', 'Install now')}</Button>}>
          {t('assistant.install.approval', '{{tasks}} need administrator approval. Install them now, or later from here or the Setup screen.', {
            tasks: approvals.map((step) => step.label).join(', '),
          })}
        </Banner>
      )}

      <SetupStepList
        steps={queue}
        hints="none"
        stateOf={(step) => STATE[statusOf(step)]}
        meta={(step) =>
          statusOf(step) === 'running' && startedAt[step.id] ? formatElapsed(now - startedAt[step.id]) : null
        }
        more={(step) => {
          if (statusOf(step) !== 'running') return null;
          // A model download shows how far it is; anything else its last line.
          const progress = runningDownload(step.progress);
          if (progress) return <DownloadBar progress={progress} named />;
          return lastLine && <span className="ui-row-line">{lastLine}</span>;
        }}
      >
        <Row state="pending" title={t('assistant.install.firstStart', 'First start: download the AI models')} />
      </SetupStepList>

      {installing && (
        <p className="ui-note">{t('assistant.install.leave', 'You can leave this running. Your PC stays awake until it finishes.')}</p>
      )}

      <div className="assistant-actions">
        {installing && (
          <Button disabled={cancelling} onClick={requestCancel}>
            {cancelling ? t('assistant.install.cancelling', 'Cancelling…') : t('assistant.install.cancel', 'Cancel')}
          </Button>
        )}
        {(cancelled || failed) && (
          <Button variant="primary" onClick={onRetry}>
            {cancelled ? t('assistant.install.resume', 'Resume') : t('assistant.install.retry', 'Retry')}
          </Button>
        )}
        {(cancelled || (failed && isNetworkCause(cause))) && (
          <Button onClick={onNetwork}>{t('assistant.install.network', 'Network settings')}</Button>
        )}
        {failed && (
          <Button onClick={copyDiagnostics}>
            {copied ? t('assistant.install.copied', 'Copied') : t('assistant.install.copy', 'Copy diagnostics')}
          </Button>
        )}
        <Button variant="link" aria-expanded={showDetails} onClick={() => setShowDetails((value) => !value)}>
          {showDetails ? t('assistant.install.hideDetails', 'Hide details') : t('assistant.install.showDetails', 'Show details')}
        </Button>
        {cancelled && (
          <Button variant="link" className="assistant-actions-end" onClick={onManual}>
            {t('assistant.welcome.manual', 'Set up manually')}
          </Button>
        )}
        {failed && (
          <Button variant="link" className="assistant-actions-end" onClick={onOpenSetup}>
            {t('assistant.install.openSetup', 'Open the Setup screen')}
          </Button>
        )}
      </div>

      {showDetails && (
        <div className="assistant-log">
          <LogViewer
            lines={lines}
            title={t('setup.output', 'Output')}
            onClear={clear}
            onReveal={() => revealSetupLogs().catch(() => undefined)}
          />
        </div>
      )}
    </div>
  );
};

export default InstallPage;

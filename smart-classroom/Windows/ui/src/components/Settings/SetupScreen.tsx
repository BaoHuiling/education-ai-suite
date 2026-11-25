// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// What is not working on this PC, and one button that fixes it; when nothing
// is, that said in the middle of the screen. Below it, closed until asked for,
// every check with its own actions, and the setup output.

import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import '../../assets/css/Setup.css';
import type { ConfigProblem } from '../../types/config';
import { SETUP_ERROR, type AutomaticRunResult, type SetupStep } from '../../types/setup';
import { fieldLabel } from '../../i18n/settingsText';
import { NO_CHANGES, useConfig, useConfigProblems } from '../../services/configManager';
import { runningDownload } from '../../services/downloadProgress';
import { toMessage } from '../../services/ipcResult';
import { fixable, listedSteps, ownRepair, rowState, runError, willRun, type RowState } from '../../services/problemList';
import { useServices } from '../../services/serviceManager';
import { classifyFailure, isNetworkCause } from '../../services/setupFailure';
import { copyText, revealSetupLogs, runAutomaticSetup, useSetup, useSetupLogs } from '../../services/setupManager';
import { blockedBy, iconOf, isActing, isRunning } from '../../services/setupStatus';
import { formatElapsed, useNow, useStartTimes } from '../../hooks/useNow';
import SetupStepList from '../setup/SetupStepList';
import { useFailureText } from '../setup/failureText';
import { Banner, Button, DownloadBar, LogViewer, PageHeader, Row, RowList, type StatusState } from '../ui';

const MARK: Partial<Record<RowState, StatusState>> = { waiting: 'pending', fixing: 'running', fixed: 'ok' };

/** Where the proxy settings are: Configuration opens their group at this field. */
const PROXY_FIELD = 'httpsProxy';

interface SetupScreenProps {
  /** Services, to watch the backend; Configuration, at one setting. */
  onOpenScreen?: (screen: 'services' | 'config', target?: string) => void;
}

const SetupScreen: React.FC<SetupScreenProps> = ({ onOpenScreen }) => {
  const { t } = useTranslation();
  const failureText = useFailureText();
  const { sections, steps, error, errorCode, busyId, checking, check, run, cancel, clearError } = useSetup();
  const { services, start } = useServices();
  const { lines, clear: clearLogs } = useSetupLogs();
  const { description } = useConfig();
  // The saved settings, without pending edits: this is about what runs.
  const { messages: settingProblems } = useConfigProblems(NO_CHANGES, description);

  // The Fix run this screen started: its queue while it runs, then what failed.
  const [queue, setQueue] = useState<string[] | null>(null);
  const [failed, setFailed] = useState<AutomaticRunResult['failed']>([]);
  const [fixError, setFixError] = useState('');
  const [showChecks, setShowChecks] = useState(false);
  const [showOutput, setShowOutput] = useState(false);
  const [copied, setCopied] = useState(false);

  const backend = services.find((service) => service.id === 'backend');
  // Setup at work, whoever started it: a row's action, Fix, or the repair the
  // app makes by itself at launch. Between two steps of a run no step is
  // acting, but the services still cannot start, which says it is not over.
  const working = !!busyId || !!queue || steps.some(isActing) || services.some((service) => service.startBlockedBy === 'setup');
  // A whole run, as opposed to one row's action.
  const autoRun = working && !busyId;

  // Cancel is disabled once clicked, until the work ends: an administrator
  // task under way cannot be stopped and runs to its end first, which can
  // take minutes (cancel() in setup-runner.cjs).
  const [cancelling, setCancelling] = useState(false);
  if (cancelling && !working) setCancelling(false);
  const requestCancel = () => {
    setCancelling(true);
    cancel().catch(() => setCancelling(false));
  };
  // A check this screen did not start, such as the one at launch.
  const checkingNow = checking || (!working && steps.some(isRunning));
  const checked = steps.some((step) => step.status !== 'unknown');

  // Sticky across checks and runs (see listedSteps). Adjusted while rendering
  // rather than in an effect, so no frame shows an empty list, which would
  // read as "nothing needs fixing".
  const [listed, setListed] = useState({ steps: [] as SetupStep[], hold: false, ids: [] as string[] });
  if (listed.steps !== steps || listed.hold !== autoRun) {
    setListed({ steps, hold: autoRun, ids: listedSteps(steps, listed.ids, autoRun) });
  }
  const rows = useMemo(
    () => listed.ids.map((id) => steps.find((step) => step.id === id)).filter((step): step is SetupStep => !!step),
    [listed.ids, steps]
  );

  const fixes = steps.filter(fixable);
  const blocked = blockedBy(steps);
  const outstanding = rows.filter((step) => rowState(step, false) !== 'fixed').length + settingProblems.length;
  const clear = checked && !working && !checkingNow && !outstanding;

  const runningId = rows.find(isActing)?.id ?? null;
  const startedAt = useStartTimes(runningId);
  const now = useNow(!!runningId, 1000);
  const lastLine = lines.length ? lines[lines.length - 1].text : '';
  const tail = useMemo(() => lines.slice(-80).map((line) => line.text), [lines]);

  const fix = async () => {
    setQueue(fixes.map((step) => step.id));
    setFailed([]);
    setFixError('');
    clearError();
    try {
      // Administrator tasks included: someone is here to approve them.
      setFailed((await runAutomaticSetup(true)).failed);
    } catch (e) {
      setFixError(toMessage(e));
    } finally {
      setQueue(null);
    }
  };

  const diagnostics = () => {
    const report = steps.map(
      (step) => `${step.status.padEnd(8)} ${step.label}${step.optional ? ' (optional)' : ''}\n           ${step.detail}`
    );
    return [`Smart Classroom setup — ${new Date().toISOString()}`, `${navigator.userAgent}`, '', ...report].join('\n');
  };

  // Nothing to fix says so itself, in the middle of the screen.
  const subtitle = !checked || (checkingNow && !rows.length)
    ? t('setup.summary.checking', 'Checking this PC…')
    : autoRun
      ? t('setup.summary.fixing', 'Fixing what is missing. This can take a while.')
      : outstanding
        ? t('setup.summary.problems', '{{count}} thing needs fixing.', { count: outstanding })
        : undefined;

  const stepRow = (step: SetupStep) => {
    const state = rowState(step, autoRun && willRun(step, queue));
    const ranInto = state === 'problem' ? runError(step, failed) : null;
    const cause = ranInto ? classifyFailure([ranInto, ...tail]) : null;
    const repair = state === 'problem' ? ownRepair(step) : undefined;
    // A model download shows how far it is; anything else its last line.
    const download = runningDownload(step.progress);
    const meta =
      state === 'fixing'
        ? startedAt[step.id] && formatElapsed(now - startedAt[step.id])
        : state === 'waiting'
          ? t('setup.row.waiting', 'Waiting')
          : state === 'fixed'
            ? t('setup.row.fixed', 'Fixed')
            : state === 'checking'
              ? t('setup.checking', 'Checking…')
              : null;

    return (
      <Row
        key={step.id}
        state={MARK[state] ?? iconOf(step)}
        title={step.label}
        badge={
          step.repairKind === 'elevated' && state !== 'fixed'
            ? t('setup.needsAdmin', 'needs administrator')
            : step.optional
              ? t('setup.optional', 'optional')
              : undefined
        }
        meta={meta}
        detail={
          <>
            {step.detail && step.detail !== ranInto && <span>{step.detail}</span>}
            {cause && <span className="ui-row-cause">{failureText(cause)}</span>}
            {ranInto && <span className="ui-mono">{ranInto}</span>}
            {state === 'fixing' &&
              (download ? <DownloadBar progress={download} named /> : lastLine && <span className="ui-row-line">{lastLine}</span>)}
            {(state === 'problem' || state === 'waiting') && blocked[step.id] && (
              <span className="ui-row-blocked">
                {t('setup.blockedBy', 'Needs {{items}} first.', { items: blocked[step.id].join(', ') })}
              </span>
            )}
          </>
        }
        actions={
          state === 'problem' && (repair || cause) ? (
            <>
              {cause && isNetworkCause(cause) && (
                <Button onClick={() => onOpenScreen?.('config', PROXY_FIELD)}>{t('setup.proxySettings', 'Proxy settings')}</Button>
              )}
              {cause === 'backendRunning' && (
                <Button onClick={() => onOpenScreen?.('services')}>{t('setup.openServices', 'Open Services')}</Button>
              )}
              {repair && (
                <Button
                  variant={repair.destructive ? 'danger' : 'secondary'}
                  disabled={working || checkingNow || !!blocked[step.id]}
                  onClick={() => void run(step.id, repair.id)}
                >
                  {repair.label}
                </Button>
              )}
            </>
          ) : undefined
        }
      />
    );
  };

  const settingRow = (problem: ConfigProblem) => {
    const field = description?.fields.find((entry) => entry.file === problem.file && entry.path === problem.path);
    return (
      <Row
        key={problem.rule}
        state="warn"
        title={field ? fieldLabel(t, field) : problem.summary}
        detail={<span>{problem.message}</span>}
        actions={<Button onClick={() => onOpenScreen?.('config', problem.path)}>{t('config.showSetting', 'Show setting')}</Button>}
      />
    );
  };

  const backendStartable = !!backend?.runnable && (backend.status === 'stopped' || backend.status === 'failed');
  const healthy = backend?.status === 'healthy';

  const more = (
    <div className="setup-more">
      <Button variant="link" aria-expanded={showOutput} onClick={() => setShowOutput((open) => !open)}>
        {showOutput ? t('setup.hideOutput', 'Hide output') : t('setup.showOutput', 'Show output')}
      </Button>
      <Button variant="link" aria-expanded={showChecks} onClick={() => setShowChecks((open) => !open)}>
        {showChecks ? t('setup.hideChecks', 'Hide all checks') : t('setup.showChecks', 'All checks ({{count}})', { count: steps.length })}
      </Button>
    </div>
  );

  return (
    <div className="setup-screen">
      {/* Centred in the screen while nothing below it is open. */}
      <div className={`setup-body${clear && !showOutput && !showChecks ? ' centered' : ''}`}>
        <PageHeader
          title={t('setup.title', 'Setup')}
          subtitle={subtitle}
          actions={
            <>
              <Button disabled={working || checkingNow} onClick={check}>
                {checkingNow ? t('setup.checking', 'Checking…') : t('setup.recheck', 'Re-check')}
              </Button>
              {working ? (
                <Button disabled={cancelling} onClick={requestCancel}>
                  {cancelling ? t('setup.cancelling', 'Cancelling…') : t('setup.cancel', 'Cancel')}
                </Button>
              ) : (
                fixes.length > 0 && (
                  <Button variant="primary" disabled={checkingNow} onClick={() => void fix()}>
                    {t('setup.runAll', 'Fix {{count}} item', { count: fixes.length })}
                  </Button>
                )
              )}
            </>
          }
        />

        {(error || fixError) && (
          <Banner
            tone="error"
            actions={
              errorCode === SETUP_ERROR.BACKEND_RUNNING && (
                <Button onClick={() => onOpenScreen?.('services')}>{t('setup.openServices', 'Open Services')}</Button>
              )
            }
          >
            {error || fixError}
          </Banner>
        )}

        {clear ? (
          <div className="setup-ready" role="status">
            <span className="setup-ready-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="40" height="40">
                <path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
            <h4 className="setup-ready-title">
              {healthy ? t('setup.clear.working', 'Everything is working') : t('setup.clear.installed', 'Everything is installed')}
            </h4>
            <p className="setup-ready-text">
              {healthy
                ? t('setup.clear.workingText', 'This PC has everything Smart Classroom needs, and the backend is running.')
                : backendStartable
                  ? t('setup.clear.stoppedText', 'Start the backend to use Smart Classroom.')
                  : t('setup.clear.installedText', 'The backend is not running yet. Services shows how it is doing.')}
            </p>
            {(backendStartable || (backend && !healthy)) && (
              <div className="setup-ready-actions">
                {backendStartable ? (
                  <Button
                    variant="primary"
                    onClick={() => {
                      // Startup is slow; Services is where its output is.
                      void start('backend');
                      onOpenScreen?.('services');
                    }}
                  >
                    {t('setup.startBackend', 'Start backend')}
                  </Button>
                ) : (
                  <Button onClick={() => onOpenScreen?.('services')}>{t('setup.openServices', 'Open Services')}</Button>
                )}
              </div>
            )}
            {more}
          </div>
        ) : (
          (rows.length > 0 || settingProblems.length > 0) && (
            <RowList label={t('setup.problems', 'What needs fixing')}>
              {rows.map(stepRow)}
              {settingProblems.map(settingRow)}
            </RowList>
          )
        )}

        {!working && rows.some((step) => step.repairKind === 'elevated' && rowState(step, false) === 'problem') && (
          <p className="ui-note">
            {t(
              'setup.adminNote',
              'Windows asks an administrator to approve the items marked “needs administrator”, once for all of them. The prompt names Windows PowerShell.'
            )}
          </p>
        )}

        {!clear && more}

        {/* The output first: it is short, and it is what matters while a fix runs. */}
        {showOutput && (
          <div className="setup-output">
            <LogViewer
              lines={lines}
              title={t('setup.output', 'Output')}
              onClear={clearLogs}
              onReveal={() => revealSetupLogs().catch(() => undefined)}
            />
          </div>
        )}

        {showChecks && (
          <section className="setup-checks" aria-label={t('setup.allChecks', 'All checks')}>
            <div className="setup-checks-tools">
              <Button
                disabled={!checked}
                onClick={async () => {
                  await copyText(diagnostics()).catch(() => undefined);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1800);
                }}
              >
                {copied ? t('setup.copied', 'Copied') : t('setup.copyDiagnostics', 'Copy diagnostics')}
              </Button>
            </div>
            <SetupStepList
              steps={steps}
              groupBy="section"
              sections={sections}
              hints="all"
              actions={{
                disabled: working || checkingNow,
                hiddenFor: busyId,
                onRun: (stepId, actionId) => void run(stepId, actionId),
              }}
            />
          </section>
        )}
      </div>
    </div>
  );
};

export default SetupScreen;

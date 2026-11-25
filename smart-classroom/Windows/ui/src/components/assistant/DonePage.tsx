// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The end of the first run: the backend's first start, which downloads the AI
// models, then what was installed and what still needs doing. A first start
// that fails, most often on a download, says why and how to try again.

import React, { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { ServiceSnapshot } from '../../types/services';
import type { SetupStep } from '../../types/setup';
import { revealLogs, useServiceLogs } from '../../services/serviceManager';
import { formatElapsed, useNow } from '../../hooks/useNow';
import { hubName, runningDownload } from '../../services/downloadProgress';
import { isNetworkCause, type FailureCause } from '../../services/setupFailure';
import { useFailureText } from '../setup/failureText';
import { Banner, Button, DownloadBar, LogViewer, PageHeader, Row, RowList } from '../ui';
import { modelDownload, startFailure, type ActiveSnapshot } from './assistantModel';

// The network page can fix these: the proxy, or the other model source.
const fixedOnNetworkPage = (cause: FailureCause) => isNetworkCause(cause) || cause === 'rateLimit';

interface DonePageProps {
  snapshot: ActiveSnapshot;
  steps: SetupStep[];
  backend: ServiceSnapshot | undefined;
  onStartBackend: () => void;
  onOpenApp: () => void;
  onOpenServices: () => void;
  /** The network page, for a first start that failed on a download. */
  onNetwork: () => void;
}

const DonePage: React.FC<DonePageProps> = ({
  snapshot,
  steps,
  backend,
  onStartBackend,
  onOpenApp,
  onOpenServices,
  onNetwork,
}) => {
  const { t } = useTranslation();
  const failureText = useFailureText();
  const [showDetails, setShowDetails] = useState(false);
  const status = backend?.status;
  const starting = status === 'starting';
  const startFailed = status === 'failed';
  const { lines } = useServiceLogs(starting || startFailed ? 'backend' : null);
  const texts = useMemo(() => lines.map((line) => line.text), [lines]);
  // What the backend reports, else what its log says it is downloading.
  const progress = starting ? runningDownload(backend?.download) : null;
  const download = progress ?? modelDownload(texts);
  const failure = useMemo(() => (startFailed ? startFailure(texts) : null), [startFailed, texts]);
  const now = useNow(starting, 1000);

  const label = (id: string) => steps.find((step) => step.id === id)?.label ?? id;
  const result = snapshot.result;
  const skipped = result?.manual ?? [];
  const failed = (result?.failed ?? []).filter((entry) => entry.id);

  return (
    <div className="assistant-page">
      <PageHeader
        focusOnMount
        title={
          status === 'healthy'
            ? t('assistant.done.readyTitle', 'Smart Classroom is ready')
            : starting
              ? t('assistant.done.startingTitle', 'Starting Smart Classroom')
              : status === 'failed'
                ? t('assistant.done.failedTitle', 'Smart Classroom did not start')
                : t('assistant.done.installedTitle', 'Smart Classroom is installed')
        }
      />

      {starting && (
        <Banner>
          <div className="assistant-countdown" role="status">
            <span className="ui-spinner" aria-hidden="true" />
            <span>
              {download
                ? t('assistant.done.downloading', 'Downloading {{model}} from {{hub}}', {
                    model: download.repo,
                    hub: hubName(download.hub),
                  })
                : t('assistant.done.starting', 'The first start downloads and prepares the AI models, so it takes a while.')}
            </span>
            {backend?.startedAt && <span className="ui-row-meta">{formatElapsed(now - backend.startedAt)}</span>}
          </div>
          {progress && <DownloadBar progress={progress} />}
        </Banner>
      )}

      {failure && (
        <Banner tone="error" actions={<Button onClick={onOpenServices}>{t('assistant.done.openServices', 'Open Services')}</Button>}>
          <p>
            {failure.cause === 'other'
              ? t('assistant.done.failed', 'The backend stopped while starting. Its log shows why.')
              : failureText(failure.cause)}
          </p>
          {(failure.error || backend?.error) && <p className="ui-mono">{failure.error || backend?.error}</p>}
        </Banner>
      )}

      <RowList label={t('assistant.done.summary', 'Summary')}>
        <Row
          state="ok"
          title={t('assistant.done.installed', 'Installed')}
          detail={
            <span>
              {result?.ran.length ? result.ran.map(label).join(', ') : t('assistant.done.nothingNew', 'Everything was already in place')}
            </span>
          }
        />
        {skipped.length > 0 && (
          <Row
            state="warn"
            title={t('assistant.done.later', 'Still to do')}
            detail={
              <span>
                {t('assistant.done.laterDetail', '{{items}}: fix them on the Setup screen when you are ready.', {
                  items: skipped.map(label).join(', '),
                })}
              </span>
            }
          />
        )}
        {failed.length > 0 && (
          <Row
            state="failed"
            title={t('assistant.done.notInstalled', 'Not installed')}
            detail={<span>{failed.map((entry) => label(entry.id!)).join(', ')}</span>}
          />
        )}
      </RowList>

      <div className="assistant-actions">
        {status === 'stopped' && <Button onClick={onStartBackend}>{t('assistant.done.start', 'Start Smart Classroom')}</Button>}
        {startFailed && (
          <Button variant="primary" onClick={onStartBackend}>
            {t('assistant.install.retry', 'Retry')}
          </Button>
        )}
        {failure && fixedOnNetworkPage(failure.cause) && (
          <Button onClick={onNetwork}>{t('assistant.install.network', 'Network settings')}</Button>
        )}
        <Button variant={startFailed ? 'secondary' : 'primary'} disabled={status !== 'healthy'} onClick={onOpenApp}>
          {t('assistant.done.open', 'Open Smart Classroom')}
        </Button>
        {startFailed && (
          <Button variant="link" aria-expanded={showDetails} onClick={() => setShowDetails((value) => !value)}>
            {showDetails ? t('assistant.install.hideDetails', 'Hide details') : t('assistant.install.showDetails', 'Show details')}
          </Button>
        )}
      </div>

      {startFailed && showDetails && (
        <div className="assistant-log">
          <LogViewer
            lines={lines}
            title={backend?.label ?? ''}
            onReveal={() => revealLogs('backend').catch(() => undefined)}
          />
        </div>
      )}
    </div>
  );
};

export default DonePage;

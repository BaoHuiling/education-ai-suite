// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// First page of the first run: what setup does, the results of the checks,
// and the countdown to the recommended installation.

import React from 'react';
import { useTranslation } from 'react-i18next';
import type { SetupStep } from '../../types/setup';
import { useNow } from '../../hooks/useNow';
import { connectionText } from '../setup/networkText';
import { Banner, Button, PageHeader, Row, RowList } from '../ui';
import DiskWarning from './DiskWarning';
import { secondsLeft, type ActiveSnapshot } from './assistantModel';

interface WelcomePageProps {
  snapshot: ActiveSnapshot;
  steps: SetupStep[];
  diskAccepted: boolean;
  onDiskAccepted: (accepted: boolean) => void;
  onInstall: () => void;
  onCustomize: () => void;
  onManual: () => void;
}

const WelcomePage: React.FC<WelcomePageProps> = ({
  snapshot,
  steps,
  diskAccepted,
  onDiskAccepted,
  onInstall,
  onCustomize,
  onManual,
}) => {
  const { t } = useTranslation();
  const checking = snapshot.phase === 'checking';
  const counting = snapshot.phase === 'countdown';
  const now = useNow(counting);
  const seconds = secondsLeft(snapshot.endsAt, now);
  const total = 15;

  const warnings = steps.filter((step) => step.status === 'warn').length;
  const network = snapshot.network;
  const hub = network?.suggestedHub ?? network?.hub ?? 'huggingface';
  // A nearly full disk stops the flow; installing then takes a deliberate yes.
  const needsDiskYes = snapshot.diskLow;

  return (
    <div className="assistant-page">
      <PageHeader
        focusOnMount
        title={t('assistant.welcome.title', 'Set up Smart Classroom')}
        subtitle={t(
          'assistant.welcome.lead',
          'Smart Classroom needs Python, the packages it runs on, and its AI models. Setup downloads several gigabytes and installs everything for you. It takes a while, and you can leave it running.'
        )}
      />

      <RowList label={t('assistant.welcome.checks', 'Checks')}>
        <Row
          state={checking ? 'running' : warnings ? 'warn' : 'ok'}
          title={t('assistant.welcome.pc', 'This PC')}
          meta={
            checking
              ? t('assistant.checking', 'Checking…')
              : warnings
                ? t('assistant.welcome.pcWarnings', 'Ready, with {{count}} warning', { count: warnings })
                : t('assistant.welcome.pcReady', 'Ready')
          }
        />
        <Row
          state={checking ? 'running' : network?.ok ? 'ok' : 'warn'}
          title={t('assistant.welcome.network', 'Network')}
          meta={checking ? t('assistant.checking', 'Checking…') : connectionText(t, network?.chosen)}
        />
        {!checking && network && (
          <Row
            state="ok"
            title={t('assistant.welcome.models', 'AI models')}
            meta={
              hub === 'modelscope'
                ? t('assistant.welcome.fromModelScope', 'Downloaded from ModelScope')
                : t('assistant.welcome.fromHuggingFace', 'Downloaded from Hugging Face')
            }
            detail={
              network.suggestedHub ? (
                <span>
                  {t('assistant.welcome.hubSwitched', '{{source}} cannot be reached from here', {
                    source: network.hub === 'modelscope' ? 'ModelScope' : 'Hugging Face',
                  })}
                </span>
              ) : undefined
            }
          />
        )}
      </RowList>

      {snapshot.diskLow && (
        <DiskWarning accepted={diskAccepted} onChange={onDiskAccepted} showAcknowledgement={needsDiskYes} />
      )}

      {counting && (
        <Banner>
          <div className="assistant-countdown" role="status">
            <span>{t('assistant.welcome.countdown', 'Installing with recommended settings in {{seconds}} s', { seconds })}</span>
            <progress max={total} value={total - seconds} aria-hidden="true" />
          </div>
        </Banner>
      )}

      <div className="assistant-actions">
        <Button variant="primary" disabled={checking || (needsDiskYes && !diskAccepted)} onClick={onInstall}>
          {t('assistant.welcome.installNow', 'Install now')}
        </Button>
        <Button onClick={onCustomize}>{t('assistant.welcome.customize', 'Customize')}</Button>
        <Button variant="link" className="assistant-actions-end" onClick={onManual}>
          {t('assistant.welcome.manual', 'Set up manually')}
        </Button>
      </div>
    </div>
  );
};

export default WelcomePage;

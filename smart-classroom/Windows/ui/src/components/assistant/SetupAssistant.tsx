// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The setup assistant: the first run.
// The main process owns the flow and decides what happens when; this shows it,
// lets the user customize, and sends their choices. It takes the whole window,
// so it brings its own title bar. Afterwards, what is not working is on the
// Setup screen, and the settings are under Configuration.

import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import '../../assets/css/Assistant.css';
import BrandSlot from '../../assets/images/BrandSlot.svg';
import type { FirstRunChoice } from '../../types/firstRun';
import { toMessage } from '../../services/ipcResult';
import { useServices } from '../../services/serviceManager';
import { useSetup } from '../../services/setupManager';
import LanguageSwitcher from '../LanguageSwitcher';
import { Banner } from '../ui';
import AdminTasksPage from './AdminTasksPage';
import DiskWarning from './DiskWarning';
import DonePage from './DonePage';
import FeaturesPage from './FeaturesPage';
import InstallPage from './InstallPage';
import NetworkPage, { type NetworkMode } from './NetworkPage';
import WelcomePage from './WelcomePage';
import { adminTasks, onboardingPage, pageOrder, railItems, type ActiveSnapshot, type AssistantPage } from './assistantModel';

export type SendChoice = (choice: FirstRunChoice, options?: { includeElevated?: boolean }) => Promise<void>;

export type AppScreen = 'setup' | 'config' | 'services';

interface SetupAssistantProps {
  /** The first-run flow. */
  snapshot: ActiveSnapshot;
  /** Sends the flow the user's choices. */
  send: SendChoice;
  /** Leave for one of the app's screens. */
  onOpenScreen: (screen: AppScreen, target?: string) => void;
  /** The backend is up: show the app. */
  onFinish: () => void;
}

const SetupAssistant: React.FC<SetupAssistantProps> = ({ snapshot, send, onOpenScreen, onFinish }) => {
  const { t } = useTranslation();
  const { steps } = useSetup();
  const { services, start } = useServices();
  const backend = services.find((service) => service.id === 'backend');

  // Where the user went, within what the phase allows; null for its default.
  const [chosen, setChosen] = useState<AssistantPage | null>(null);
  const [approveAdmin, setApproveAdmin] = useState(true);
  const [diskAccepted, setDiskAccepted] = useState(false);
  const [error, setError] = useState('');

  // A new installation starts the pages over.
  useEffect(() => {
    if (snapshot.phase === 'installing') setChosen(null);
  }, [snapshot.phase]);

  const admin = useMemo(() => adminTasks(steps), [steps]);
  const order = pageOrder(admin.length > 0);
  const page: AssistantPage = onboardingPage(snapshot, chosen) ?? 'welcome';

  const act = (choice: FirstRunChoice, options?: { includeElevated?: boolean }) => {
    setError('');
    send(choice, options).catch((e) => setError(toMessage(e)));
  };
  const install = (includeElevated = false) => act('installNow', { includeElevated });
  // Customize from the countdown keeps the user on the page they acted on.
  const stopCountdown = (stay: AssistantPage) => {
    setChosen(stay);
    act('customize');
  };

  // Without a deliberate yes, a nearly full disk keeps the installation from
  // starting; and the flow installs only once its checks are done.
  const diskBlocked = snapshot.diskLow && !diskAccepted;
  const installBlocked = diskBlocked || snapshot.phase === 'checking';
  // That yes is given on the page whose button installs.
  const diskNotice = snapshot.diskLow ? (
    <DiskWarning accepted={diskAccepted} onChange={setDiskAccepted} showAcknowledgement />
  ) : null;

  const renderPage = () => {
    switch (page) {
      case 'welcome':
        return (
          <WelcomePage
            snapshot={snapshot}
            steps={steps}
            diskAccepted={diskAccepted}
            onDiskAccepted={setDiskAccepted}
            onInstall={() => install(false)}
            onCustomize={() => stopCountdown('network')}
            onManual={() => act('manual')}
          />
        );
      case 'network': {
        const mode: NetworkMode = snapshot.held === 'customize' || snapshot.held === 'network' ? 'customize' : 'retry';
        // From Done: the installation worked and the backend's first start did
        // not, so trying again means starting the backend, not installing.
        const restart = snapshot.phase === 'finished' && !!snapshot.result?.envOk;
        const retry = () => {
          if (!restart) return install(snapshot.includeElevated);
          setChosen(null);
          void start('backend');
        };
        return (
          <NetworkPage
            mode={mode}
            snapshot={snapshot}
            onBack={() => setChosen(mode === 'retry' ? null : 'welcome')}
            onContinue={() => (mode === 'retry' ? retry() : setChosen('features'))}
          />
        );
      }
      case 'features':
        return (
          <FeaturesPage
            onBack={() => setChosen('network')}
            onContinue={() => (admin.length ? setChosen('admin') : install(false))}
            continueLabel={admin.length ? t('assistant.next', 'Next') : t('assistant.installButton', 'Install')}
            continueDisabled={!admin.length && installBlocked}
            notice={admin.length ? null : diskNotice}
          />
        );
      case 'admin':
        return (
          <AdminTasksPage
            tasks={admin}
            approve={approveAdmin}
            onApproveChange={setApproveAdmin}
            onBack={() => setChosen('features')}
            onInstall={() => install(approveAdmin)}
            installDisabled={installBlocked}
            notice={diskNotice}
          />
        );
      case 'install':
        return (
          <InstallPage
            snapshot={snapshot}
            steps={steps}
            onApprove={() => act('approveElevated')}
            onRetry={() => install(snapshot.includeElevated)}
            onNetwork={() => setChosen('network')}
            onManual={() => act('manual')}
            onOpenSetup={() => onOpenScreen('setup')}
          />
        );
      case 'done':
        return (
          <DonePage
            snapshot={snapshot}
            steps={steps}
            backend={backend}
            onStartBackend={() => void start('backend')}
            onOpenApp={onFinish}
            onOpenServices={() => onOpenScreen('services')}
            onNetwork={() => setChosen('network')}
          />
        );
      default:
        return null;
    }
  };

  return (
    <div className="assistant">
      <header className="assistant-header">
        <img src={BrandSlot} alt="Intel Logo" className="assistant-logo" />
        <span className="assistant-title">{t('assistant.title', 'Smart Classroom setup')}</span>
        <div className="assistant-header-actions">
          <LanguageSwitcher />
        </div>
      </header>
      <div className="assistant-body">
        <nav className="assistant-rail" aria-label={t('assistant.steps', 'Setup steps')}>
          <ol>
            {railItems(order, page).map((item) => (
              <li key={item.id} className={`assistant-rail-item ${item.state}`} aria-current={item.state === 'current' ? 'step' : undefined}>
                <span className="assistant-rail-mark" aria-hidden="true">
                  {item.state === 'complete' ? '✓' : ''}
                </span>
                {t(`assistant.pages.${item.id}`, item.id)}
              </li>
            ))}
          </ol>
        </nav>
        <main className="assistant-main">
          {error && <Banner tone="error">{error}</Banner>}
          {renderPage()}
        </main>
      </div>
    </div>
  );
};

export default SetupAssistant;

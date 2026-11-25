// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Steps that need administrator approval. They are approved or skipped before
// the unattended part starts, and approved ones run first, so Windows asks
// while the user is still here. It asks once for all of them: they run in one
// elevated PowerShell (electron/services/elevation.cjs).

import React from 'react';
import { useTranslation } from 'react-i18next';
import type { SetupStep } from '../../types/setup';
import SetupStepList from '../setup/SetupStepList';
import { Button, PageHeader } from '../ui';

interface AdminTasksPageProps {
  tasks: SetupStep[];
  approve: boolean;
  onApproveChange: (approve: boolean) => void;
  onBack: () => void;
  onInstall: () => void;
  installDisabled?: boolean;
  /** Shown above the buttons, such as the disk space warning. */
  notice?: React.ReactNode;
}

const AdminTasksPage: React.FC<AdminTasksPageProps> = ({
  tasks,
  approve,
  onApproveChange,
  onBack,
  onInstall,
  installDisabled,
  notice,
}) => {
  const { t } = useTranslation();

  const purpose = (step: SetupStep) =>
    step.id === 'longPaths'
      ? t('assistant.admin.longPaths', 'Lets downloads with deep folder paths succeed.')
      : step.id === 'dlStreamer'
        ? t('assistant.admin.dlStreamer', 'Runs the video analytics pipelines.')
        : null;

  return (
    <div className="assistant-page">
      <PageHeader
        focusOnMount
        title={t('assistant.pages.admin', 'Administrator tasks')}
        subtitle={t(
          'assistant.admin.lead',
          'These change Windows itself, so Windows asks an administrator to approve them, once for all of them. The prompt names Windows PowerShell, which runs them. They run first, while you are here; the rest needs nobody.'
        )}
      />

      <SetupStepList steps={tasks} hints="links" more={(step) => purpose(step) && <span>{purpose(step)}</span>} />

      <div className="assistant-radios" role="radiogroup">
        <label className="assistant-check">
          <input type="radio" name="assistant-admin" checked={approve} onChange={() => onApproveChange(true)} />
          {tasks.length > 1
            ? t('assistant.admin.approveAll', 'Install them first. Windows asks for approval once, for all of them.')
            : t('assistant.admin.approveOne', 'Install it first. Windows asks for approval once.')}
        </label>
        <label className="assistant-check">
          <input type="radio" name="assistant-admin" checked={!approve} onChange={() => onApproveChange(false)} />
          {t('assistant.admin.skip', 'Skip for now. What needs them will not work until they are installed here or on the Setup screen.')}
        </label>
      </div>

      {notice}

      <div className="assistant-actions">
        <Button onClick={onBack}>{t('assistant.back', 'Back')}</Button>
        <Button variant="primary" disabled={installDisabled || !tasks.length} onClick={onInstall}>
          {t('assistant.installButton', 'Install')}
        </Button>
      </div>
    </div>
  );
};

export default AdminTasksPage;

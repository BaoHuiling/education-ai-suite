// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// What to use, chosen by group, each group expandable into its features and
// its settings: the same FeaturePicker as the Configuration screen.
// Dependencies follow the Configuration screen's rules, so a combination the
// backend would undo is offered as a fix.

import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useConfig } from '../../services/configManager';
import { useSettingsDraft } from '../../services/settingsDraft';
import FeaturePicker from '../setup/FeaturePicker';
import ProblemBanners from '../setup/ProblemBanners';
import { Banner, Button, PageHeader } from '../ui';

interface FeaturesPageProps {
  onBack: () => void;
  /** Saved; go on. */
  onContinue: () => void;
  continueLabel: string;
  continueDisabled?: boolean;
  /** Shown above the buttons, such as the disk space warning when this page installs. */
  notice?: React.ReactNode;
}

const FeaturesPage: React.FC<FeaturesPageProps> = ({ onBack, onContinue, continueLabel, continueDisabled, notice }) => {
  const { t } = useTranslation();
  const { description, save, saving, error } = useConfig();

  // The toggles and the settings the groups hold.
  const fields = useMemo(() => {
    const groups = new Set(description?.featureGroups.map((group) => group.id));
    return description?.fields.filter((field) => field.group === 'features' || (field.onboarding && groups.has(field.onboarding))) ?? [];
  }, [description]);
  const { draft, changes, blocking, byField, messages, setValue, applyFix, canApply, reset } = useSettingsDraft(description, fields);

  const commit = async () => {
    if (changes.length && !(await save(changes))) return false;
    reset();
    return true;
  };

  return (
    <div className="assistant-page">
      <PageHeader
        focusOnMount
        title={t('assistant.pages.features', 'Features')}
        subtitle={t(
          'assistant.features.lead',
          'Choose what you will use. Setup only downloads what these need. Open a group for its features and settings.'
        )}
      />

      {error && <Banner tone="error">{error}</Banner>}
      <ProblemBanners messages={messages} canApply={canApply} onApplyFix={applyFix} />

      {description && (
        <FeaturePicker description={description} draft={draft} byField={byField} withSettings onChange={setValue} />
      )}

      {notice}

      <div className="assistant-actions">
        <Button disabled={saving} onClick={onBack}>
          {t('assistant.back', 'Back')}
        </Button>
        <Button
          variant="primary"
          disabled={saving || !description || blocking.length > 0 || continueDisabled}
          onClick={async () => (await commit()) && onContinue()}
        >
          {continueLabel}
        </Button>
      </div>
    </div>
  );
};

export default FeaturesPage;

// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { useTranslation } from 'react-i18next';
import { Banner } from '../ui';

interface DiskWarningProps {
  accepted: boolean;
  onChange: (accepted: boolean) => void;
  /** Once nothing counts down, installing on a nearly full disk takes a deliberate yes. */
  showAcknowledgement: boolean;
}

/** Free disk space is below what the installation and the models need. */
const DiskWarning: React.FC<DiskWarningProps> = ({ accepted, onChange, showAcknowledgement }) => {
  const { t } = useTranslation();
  return (
    <Banner tone="warning">
      <p>
        {t(
          'assistant.disk.warning',
          'Less than 50 GB of disk space is free. The installation and the AI models need about that much, and running out part-way is hard to recover from.'
        )}
      </p>
      {showAcknowledgement && (
        <label className="assistant-check">
          <input type="checkbox" checked={accepted} onChange={(event) => onChange(event.target.checked)} />
          {t('assistant.disk.accept', 'Continue anyway')}
        </label>
      )}
    </Banner>
  );
};

export default DiskWarning;

// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { useTranslation } from 'react-i18next';
import type { DownloadProgress } from '../../types/download';
import { formatBytes, fractionDone, hubName, timeLeft } from '../../services/downloadProgress';

interface DownloadBarProps {
  progress: DownloadProgress;
  /** Name the model in the text, where nothing beside the bar does. */
  named?: boolean;
}

/** A model download under way: a bar, then how much, how fast and how long is left. */
const DownloadBar: React.FC<DownloadBarProps> = ({ progress, named = false }) => {
  const { t } = useTranslation();
  const fraction = fractionDone(progress);
  const left = timeLeft(progress);

  const text = [
    named ? progress.repo : null,
    fraction === null
      ? t('services.download.starting', 'Starting the download…')
      : t('services.download.sizes', '{{done}} of {{total}}', {
          done: formatBytes(progress.done),
          total: formatBytes(progress.total),
        }),
    progress.speed ? t('services.download.speed', '{{speed}}/s', { speed: formatBytes(progress.speed) }) : null,
    !left
      ? null
      : left.unit === 'seconds'
        ? t('services.download.secondsLeft', 'less than a minute left')
        : left.unit === 'minutes'
          ? t('services.download.minutesLeft', 'about {{minutes}} min left', { minutes: left.minutes })
          : left.minutes
            ? t('services.download.hoursLeft', 'about {{hours}} h {{minutes}} min left', left)
            : t('services.download.wholeHoursLeft', 'about {{hours}} h left', left),
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div className="ui-download">
      {/* No value while the size is unknown: an indeterminate bar. Not a live
          region, which would read out every update. */}
      <progress
        className="ui-download-bar"
        max={1}
        value={fraction ?? undefined}
        aria-label={t('services.download.label', 'Downloading {{model}} from {{hub}}', {
          model: progress.repo,
          hub: hubName(progress.hub),
        })}
        aria-valuetext={text}
      />
      <span className="ui-download-text">{text}</span>
    </div>
  );
};

export default DownloadBar;

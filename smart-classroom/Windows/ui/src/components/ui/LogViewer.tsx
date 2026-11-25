// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { LogLine } from '../../types/services';
import Button from './Button';

// Presentational on purpose: the Services screen feeds it one service's log and
// the Setup screen feeds it the setup log, so filter/follow/clear/open-folder
// exist once rather than twice.
interface LogViewerProps {
  lines: LogLine[];
  title: string;
  onClear?: () => void;
  onReveal?: () => void;
}

const LogViewer: React.FC<LogViewerProps> = ({ lines, title, onClear, onReveal }) => {
  const { t } = useTranslation();
  const [filter, setFilter] = useState('');
  const [follow, setFollow] = useState(true);
  const bodyRef = useRef<HTMLDivElement>(null);

  const visible = useMemo(() => {
    if (!filter.trim()) return lines;
    const needle = filter.toLowerCase();
    return lines.filter((line) => line.text.toLowerCase().includes(needle));
  }, [lines, filter]);

  useEffect(() => {
    if (follow && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [visible, follow]);

  return (
    <div className="ui-log">
      <div className="ui-log-header">
        <span className="ui-log-title">
          {t('services.logs.title', 'Logs')}
          {title ? ` — ${title}` : ''}
        </span>
        <input
          className="ui-log-filter"
          value={filter}
          placeholder={t('services.logs.filter', 'Filter…')}
          onChange={(event) => setFilter(event.target.value)}
        />
        <label className="ui-log-follow">
          <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
          {t('services.logs.follow', 'Follow')}
        </label>
        <Button onClick={onClear} disabled={!onClear}>
          {t('services.logs.clear', 'Clear')}
        </Button>
        {onReveal && (
          <Button onClick={onReveal}>
            {t('services.logs.openFolder', 'Open folder')}
          </Button>
        )}
      </div>
      <div className="ui-log-body" ref={bodyRef}>
        {visible.length === 0 ? (
          <div className="ui-log-empty">{t('services.logs.empty', 'No output captured yet.')}</div>
        ) : (
          visible.map((line) => (
            <div key={line.seq} className={`ui-log-line stream-${line.stream}`}>
              <span className="ui-log-time">{new Date(line.ts).toLocaleTimeString()}</span>
              <span className="ui-log-text">{line.text}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

export default LogViewer;

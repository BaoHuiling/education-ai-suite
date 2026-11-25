// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The round button on the divider that hides and shows the right panel, on the
// main and grading screens. Its chevron is the Accordion's, and turns the same
// way: right to hide the panel, left to bring it back.

import React from 'react';
import { useTranslation } from 'react-i18next';

interface PanelToggleProps {
  collapsed: boolean;
  /** Where the divider is (usePanelDividerX's arrowLeft); unused when collapsed. */
  left: string;
  /** usePanelDividerX's arrowTransition: 'none' while the window resizes. */
  transition: string | undefined;
  onToggle: () => void;
}

const PanelToggle: React.FC<PanelToggleProps> = ({ collapsed, left, transition, onToggle }) => {
  const { t } = useTranslation();
  const label = collapsed ? t('accordion.showPanel', 'Show panel') : t('accordion.hidePanel', 'Hide panel');
  return (
    <button
      type="button"
      className={`arrow${collapsed ? ' collapsed' : ''}`}
      // When collapsed the divider is the container's own right edge, so
      // `.arrow.collapsed` parks the toggle just inside it instead.
      style={{ left: collapsed ? undefined : left, transition }}
      aria-expanded={!collapsed}
      aria-label={label}
      title={label}
      onClick={onToggle}
    >
      <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true">
        <path d="M6 3.5 10.5 8 6 12.5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </button>
  );
};

export default PanelToggle;

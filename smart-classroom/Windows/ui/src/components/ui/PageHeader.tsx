// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useRef } from 'react';

interface PageHeaderProps {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  /** Move keyboard focus to the title on arrival, for pages that replace each other. */
  focusOnMount?: boolean;
}

/** The title, subtitle and actions at the top of a screen or page. */
const PageHeader: React.FC<PageHeaderProps> = ({ title, subtitle, actions, focusOnMount }) => {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (focusOnMount) ref.current?.focus();
  }, [focusOnMount]);
  return (
    <div className="ui-page-header">
      <div>
        <h3 ref={ref} tabIndex={focusOnMount ? -1 : undefined} className="ui-page-title">
          {title}
        </h3>
        {subtitle && <span className="ui-page-subtitle">{subtitle}</span>}
      </div>
      {actions && <div className="ui-page-actions">{actions}</div>}
    </div>
  );
};

export default PageHeader;

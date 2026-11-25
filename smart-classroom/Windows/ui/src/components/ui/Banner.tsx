// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';

export type BannerTone = 'info' | 'success' | 'warning' | 'error';

interface BannerProps {
  tone?: BannerTone;
  /** Buttons at the end of the banner. */
  actions?: React.ReactNode;
  children: React.ReactNode;
}

/** A message across the page: information, success, a warning or an error. */
const Banner: React.FC<BannerProps> = ({ tone = 'info', actions, children }) => (
  <div className={`ui-banner ui-banner-${tone}`} role={tone === 'error' ? 'alert' : undefined}>
    <div className="ui-banner-body">{children}</div>
    {actions && <div className="ui-banner-actions">{actions}</div>}
  </div>
);

export default Banner;

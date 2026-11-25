// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';

/** A titled part of a page. */
const Section: React.FC<{ title?: React.ReactNode; children: React.ReactNode }> = ({ title, children }) => (
  <section className="ui-section">
    {title && <h4 className="ui-section-title">{title}</h4>}
    {children}
  </section>
);

export default Section;

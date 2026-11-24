// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { defaultUrlTransform } from 'react-markdown';
import { BASE_URL as API_BASE_URL } from '../services/api';

/**
 * Point the report's own links at the backend that serves them.
 *
 * Report markdown carries image URLs like `/report/{session}/mindmap-image`,
 * written root-relative because the generator does not know what host will
 * render them. Left alone they resolve against the *frontend* origin (the Vite
 * dev server or the static UI server) and 404, which is why the mind-map
 * section of a report comes out as a broken image. API_BASE_URL is the same
 * backend api.ts talks to, so this also works from a browser on another host.
 */
export const reportUrlTransform = ((url: string) => {
  if (url.startsWith('data:image/')) return url;
  if (url.startsWith('/report/')) {
    return `${API_BASE_URL}${url}`;
  }
  return defaultUrlTransform(url);
}) as any;

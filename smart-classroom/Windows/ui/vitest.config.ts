// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from 'vitest/config';

// Renderer logic tests. Run them with `npm run test:renderer`, which goes
// through tests/renderer/run-vitest.cjs to avoid a Windows drive-letter
// problem. The Electron main process has its own: `npm run test:electron`.
export default defineConfig({
  test: {
    include: ['tests/renderer/**/*.test.ts'],
    environment: 'node',
  },
});

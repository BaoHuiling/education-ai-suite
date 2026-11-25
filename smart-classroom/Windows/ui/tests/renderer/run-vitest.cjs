// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Runs vitest from a working directory spelled with an uppercase drive letter.
//
// VS Code's terminal on Windows can start in "c:\...". Node then loads vitest
// from that spelling, Vite resolves the tests' own import of vitest from
// "C:\...", the two are different modules to Node, and every suite fails with
// "Vitest failed to find the current suite". Spawning vitest from the
// uppercase spelling gives one module, whatever the terminal started in.

const { spawnSync } = require('child_process');
const path = require('path');

const ui = path.resolve(__dirname, '..', '..').replace(/^[a-z]:/, (drive) => drive.toUpperCase());
const cli = path.join(ui, 'node_modules', 'vitest', 'vitest.mjs');

const result = spawnSync(process.execPath, [cli, 'run', ...process.argv.slice(2)], { cwd: ui, stdio: 'inherit' });
process.exit(result.status ?? 1);

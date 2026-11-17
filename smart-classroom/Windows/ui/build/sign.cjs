// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// electron-builder custom Windows sign hook (win.signtoolOptions.sign).
//
// electron-builder calls this for every binary it signs: the app exe (after
// resource editing), Electron DLLs and native addons (via win.signExts),
// elevate.exe, the NSIS uninstaller (before it is embedded in the installer)
// and the final installer.
//
// Signing is opt-in: set SC_CODESIGN_SCRIPT to a PowerShell script that takes
// the file path as its only positional argument. Without it the hook is a
// no-op so local builds stay unsigned.

const { execFileSync } = require('node:child_process');
const fs = require('node:fs');

function signatureStatus(file) {
  return execFileSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', '(Get-AuthenticodeSignature -LiteralPath $env:SC_SIGN_TARGET).Status'],
    { env: { ...process.env, SC_SIGN_TARGET: file }, encoding: 'utf8' },
  ).trim();
}

async function sign({ path: file }) {
  const script = process.env.SC_CODESIGN_SCRIPT;
  if (!script) return;
  if (!fs.existsSync(script)) throw new Error(`SC_CODESIGN_SCRIPT not found: ${script}`);

  // Keep existing vendor signatures (Microsoft DirectX DLLs, prebuilt Intel DLLs).
  if (signatureStatus(file) === 'Valid') {
    console.log(`  • already signed, skipping  file=${file}`);
    return;
  }

  execFileSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', script, file],
    { stdio: 'inherit' },
  );

  const status = signatureStatus(file);
  if (status !== 'Valid') throw new Error(`Signature is ${status} after signing: ${file}`);
}

module.exports = sign;
module.exports.default = sign;

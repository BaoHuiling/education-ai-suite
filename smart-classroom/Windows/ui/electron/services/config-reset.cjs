// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function writeAtomic(file, contents) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.tmp`;
  fs.writeFileSync(temp, contents);
  fs.renameSync(temp, file);
}

function resetToShippedDefaults({ userFile, defaultFile, stampFile, backupDir, label, now = () => new Date() }) {
  try {
    if (!userFile || !stampFile || !fs.existsSync(defaultFile)) return { status: 'skipped' };

    const shipped = fs.readFileSync(defaultFile);
    const digest = sha256(shipped);
    const recorded = fs.existsSync(stampFile) ? fs.readFileSync(stampFile, 'utf8').trim() : null;
    if (recorded === digest) return { status: 'current' };

    let backup;
    if (fs.existsSync(userFile) && !fs.readFileSync(userFile).equals(shipped)) {
      fs.mkdirSync(backupDir, { recursive: true });
      const stamp = now().toISOString().replace(/[:.]/g, '-');
      backup = path.join(backupDir, `pre-update.${label}.${stamp}.bak`);
      fs.copyFileSync(userFile, backup);
      writeAtomic(userFile, shipped);
    }
    writeAtomic(stampFile, `${digest}\n`);
    return backup ? { status: 'reset', backup } : { status: 'recorded' };
  } catch (error) {
    return { status: 'failed', error: error.message };
  }
}

module.exports = { resetToShippedDefaults };

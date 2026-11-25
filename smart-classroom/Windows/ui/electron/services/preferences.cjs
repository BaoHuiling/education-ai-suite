// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// UI preferences that must survive a restart. The renderer's own storage does
// not: the packaged UI is served from a new loopback port at every launch, and
// localStorage belongs to the origin, port included. So they live here, in
// state/preferences.json.

const fs = require('fs');
const path = require('path');

const LANGUAGES = ['en', 'zh'];

/**
 * The UI language at startup (first-run wizard design, D7): the one the user
 * last chose, else the first supported language Windows prefers, else English.
 */
function pickLanguage(saved, systemLanguages = []) {
  if (LANGUAGES.includes(saved)) return saved;
  for (const language of systemLanguages) {
    // 'zh-Hans-CN', 'zh-CN', 'zh' all mean Chinese here.
    const base = String(language).toLowerCase().split(/[-_]/)[0];
    if (LANGUAGES.includes(base)) return base;
  }
  return 'en';
}

function readPreferences(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    return data && typeof data === 'object' && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}

function writePreferences(file, preferences) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(preferences, null, 2)}\n`);
  fs.renameSync(temporary, file);
}

/** Remember an explicit language choice. Throws on anything unsupported. */
function saveLanguage(file, language) {
  if (!LANGUAGES.includes(language)) throw new Error(`Unsupported language: ${String(language).slice(0, 16)}`);
  writePreferences(file, { ...readPreferences(file), language });
  return language;
}

module.exports = { LANGUAGES, pickLanguage, readPreferences, writePreferences, saveLanguage };

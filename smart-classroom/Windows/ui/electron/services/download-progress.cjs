// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Byte-level progress of model downloads, as the backend and the setup scripts
// report it (utils/download_progress.py): with SC_PROGRESS_EVENTS=1 in their
// environment they print a line like
//
//   @@download {"repo":"Qwen/Qwen3-8B","hub":"huggingface","state":"running",
//               "done":1234,"total":5678,"speed":901.2,"files":3,"fileTotal":12}
//
// at most twice a second. The process manager and the setup runner take those
// lines out of the log and keep the latest one on the service or step, where
// the screens draw it as a progress bar.

const PREFIX = '@@download ';

/** What a child needs in its environment to report progress. */
const PROGRESS_ENV = { SC_PROGRESS_EVENTS: '1' };

const STATES = ['running', 'done', 'failed'];

const count = (value) => (Number.isFinite(value) && value >= 0 ? Math.floor(value) : null);

/**
 * The progress on a line, or null for any other line. The line comes from a
 * child process, so every field is checked and only those listed are kept.
 */
function parseProgressLine(text) {
  if (typeof text !== 'string' || !text.startsWith(PREFIX)) return null;
  let raw;
  try {
    raw = JSON.parse(text.slice(PREFIX.length));
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object') return null;
  const done = count(raw.done);
  const total = count(raw.total);
  if (typeof raw.repo !== 'string' || !raw.repo || !STATES.includes(raw.state) || done === null || total === null) {
    return null;
  }
  return {
    repo: raw.repo.slice(0, 200),
    hub: typeof raw.hub === 'string' ? raw.hub.slice(0, 40) : '',
    state: raw.state,
    done,
    total: Math.max(total, done),
    speed: Number.isFinite(raw.speed) && raw.speed >= 0 ? raw.speed : null,
    files: count(raw.files) ?? 0,
    fileTotal: count(raw.fileTotal),
  };
}

/** A line that only carries progress, so it stays out of the log. */
const isProgressLine = (text) => typeof text === 'string' && text.startsWith(PREFIX);

module.exports = { PREFIX, PROGRESS_ENV, parseProgressLine, isProgressLine };

// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Byte-level progress of a model download. Kept in sync with
// electron/services/download-progress.cjs, which reads it from the backend's
// and the setup scripts' output.

export interface DownloadProgress {
  /** The model, such as "Qwen/Qwen3-8B". */
  repo: string;
  /** 'huggingface' or 'modelscope'. */
  hub: string;
  state: 'running' | 'done' | 'failed';
  /** Bytes downloaded so far. */
  done: number;
  /**
   * Bytes in the files started so far. It grows as files start, so it is the
   * whole download only once the last file is under way.
   */
  total: number;
  /** Bytes a second over the last few seconds; null until there is enough to tell. */
  speed: number | null;
  /** Files finished. */
  files: number;
  /** Files to download, when the hub says. */
  fileTotal: number | null;
}

// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Why a setup step most likely failed, read from its error and the setup
// output, so a screen can say what to do instead of only showing the error.
// The first-run installation and the Setup screen both use it, and so does the
// first run's Done page for a backend whose first start failed.

export type FailureCause =
  | 'certificate'
  | 'network'
  | 'rateLimit'
  | 'disk'
  | 'backendRunning'
  | 'filesInUse'
  | 'declined'
  | 'other';

const FAILURE_PATTERNS: [FailureCause, RegExp][] = [
  // Before network: a certificate failure also reads "Max retries exceeded".
  ['certificate', /CERTIFICATE_VERIFY_FAILED|SSLCertVerificationError|certificate verify failed|self[- ]signed certificate|unable to get local issuer/i],
  ['disk', /No space left on device|\[Errno 28\]|ERROR_DISK_FULL|not enough space on the disk|0x80070070/i],
  // The setup runner's own refusal, which has a way out: stop the backend.
  ['backendRunning', /Stop the backend first/i],
  ['filesInUse', /used by another process|\[WinError 32\]|\[WinError 5\]|Access is denied/i],
  // A model hub turning this network away for a while: ModelScope's 403
  // "操作过于频繁" (too frequent), which model_download_helper.py explains as
  // rate limiting, and HTTP 429.
  ['rateLimit', /操作过于频繁|rate-limiting this machine|Too Many Requests|\b429 Client Error/i],
  ['network', /ProxyError|ConnectTimeout|ReadTimeout|Max retries exceeded|NewConnectionError|getaddrinfo failed|Name or service not known|Temporary failure in name resolution|Network is unreachable|Connection (?:refused|reset|aborted)|Could not fetch URL|No matching distribution found|0x80072ee2|0x80072efd|0x80072f8f|Download failed|ConnectError|actively refused|did not properly respond|Proxy Authentication Required|check your internet connection/i],
  // The administrator prompt was declined (DECLINED_MESSAGE in
  // electron/services/elevation.cjs). Last: in the output it can be an earlier
  // step's, and says nothing about a later failure.
  ['declined', /Administrator approval was declined/i],
];

/**
 * What a failed setup step most likely ran into: from its error, the first
 * text, when that says; otherwise from the error and the output together. The
 * output can hold an earlier step's failure, so it is only the fallback.
 * `causes` limits the answer to the causes a screen can explain.
 */
export function classifyFailure(texts: string[], causes?: FailureCause[]): FailureCause {
  const patterns = causes ? FAILURE_PATTERNS.filter(([cause]) => causes.includes(cause)) : FAILURE_PATTERNS;
  const causeOf = (text: string) => patterns.find(([, pattern]) => pattern.test(text))?.[0];
  return causeOf(texts[0] ?? '') ?? causeOf(texts.join('\n')) ?? 'other';
}

/** Causes the proxy settings can do something about. */
export const isNetworkCause = (cause: FailureCause) => cause === 'network' || cause === 'certificate';

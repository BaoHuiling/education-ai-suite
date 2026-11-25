// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Proxy values between the settings draft, the saved file and what the
// first-run network test found. Pure, for tests/renderer.

import type { ConfigField, ConfigValue } from '../../types/config';
import type { FirstRunNetwork, ProxyValues } from '../../types/firstRun';

export const DEFAULT_NO_PROXY = 'localhost,127.0.0.1';

const KEYS: (keyof ProxyValues)[] = ['httpProxy', 'httpsProxy', 'noProxy'];

const proxyField = (fields: ConfigField[], key: keyof ProxyValues) =>
  fields.find((field) => field.file === 'proxy' && field.path === key);

/** The proxy values as saved. */
export function savedProxy(fields: ConfigField[]): ProxyValues {
  const value = (key: keyof ProxyValues) => String(proxyField(fields, key)?.value ?? '');
  return { httpProxy: value('httpProxy'), httpsProxy: value('httpsProxy'), noProxy: value('noProxy') };
}

/** The proxy values a draft would save: its edits over what is saved. */
export function proxyValues(fields: ConfigField[], draft: Record<string, ConfigValue>): ProxyValues {
  const value = (key: keyof ProxyValues) => {
    const field = proxyField(fields, key);
    return field ? String(draft[`proxy:${key}`] ?? field.value ?? '') : '';
  };
  return { httpProxy: value('httpProxy'), httpsProxy: value('httpsProxy'), noProxy: value('noProxy') };
}

/**
 * What the proxy form starts with: the configuration the first-run test found
 * working when its values can be shown, else what is saved. Credentials never
 * reach the window, so a candidate with masked ones falls back to the saved values.
 */
export function proxyPrefill(network: FirstRunNetwork | null, saved: ProxyValues): ProxyValues {
  const chosen = network?.chosen;
  if (!chosen || chosen.source === 'saved') return saved;
  if (chosen.source === 'direct') return { httpProxy: '', httpsProxy: '', noProxy: saved.noProxy };
  if ([chosen.httpProxy, chosen.httpsProxy].some((value) => value.includes('***@'))) return saved;
  return {
    httpProxy: chosen.httpProxy,
    httpsProxy: chosen.httpsProxy,
    noProxy: chosen.noProxy || saved.noProxy || DEFAULT_NO_PROXY,
  };
}

/** The draft entries that turn `saved` into `values`; none for what is unchanged. */
export function proxyDraft(values: ProxyValues, saved: ProxyValues): Record<string, ConfigValue> {
  const draft: Record<string, ConfigValue> = {};
  for (const key of KEYS) {
    if (values[key].trim() !== saved[key].trim()) draft[`proxy:${key}`] = values[key].trim();
  }
  return draft;
}

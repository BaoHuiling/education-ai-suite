// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The shared settings sections' pure parts: feature groups and proxy values.

import { describe, expect, it } from 'vitest';
import { featurePath, groupFields, groupState } from '../../src/components/setup/featureModel';
import { proxyDraft, proxyPrefill, proxyValues, savedProxy } from '../../src/components/setup/proxyModel';
import type { ConfigField, FeatureGroup } from '../../src/types/config';
import type { FirstRunNetwork } from '../../src/types/firstRun';

const field = (file: string, path: string, value: ConfigField['value'], patch: Partial<ConfigField> = {}): ConfigField =>
  ({ file, path, value, group: 'x', subgroup: null, label: path, type: 'boolean', options: null, suggestions: null, appliesWhen: null, missing: false, onboarding: null, help: null, min: null, max: null, ...patch }) as ConfigField;

const recording: FeatureGroup = { id: 'recording', label: 'Classroom recording', detail: '', features: ['asr', 'summary'] };

describe('feature groups', () => {
  const fields = [
    field('config', featurePath('asr'), true),
    field('config', featurePath('summary'), false),
    field('config', featurePath('content_search'), true),
    field('config', 'models.asr.provider', 'openai', { type: 'enum', onboarding: 'recording' }),
  ];

  it('finds a group\'s toggles in its own order', () => {
    expect(groupFields(recording, fields).map((entry) => entry.path)).toEqual(['features.asr.enabled', 'features.summary.enabled']);
  });

  it('reads on, off or mixed, from pending edits over saved values', () => {
    const members = groupFields(recording, fields);
    expect(groupState(members, (entry) => entry.value !== false)).toBe('mixed');
    expect(groupState(members, () => true)).toBe('on');
    expect(groupState(members, () => false)).toBe('off');
    expect(groupState([], () => true)).toBe('off');
  });
});

describe('proxy values', () => {
  const fields = [
    field('proxy', 'httpProxy', 'http://saved:1', { type: 'url' }),
    field('proxy', 'httpsProxy', 'http://saved:1', { type: 'url' }),
    field('proxy', 'noProxy', 'intranet', { type: 'string' }),
  ];
  const saved = savedProxy(fields);

  const network = (chosen: Partial<NonNullable<FirstRunNetwork['chosen']>> | null): FirstRunNetwork => ({
    ok: true,
    packages: true,
    installers: true,
    needInstallers: false,
    hubs: { huggingface: true, modelscope: true },
    hub: 'huggingface',
    suggestedHub: null,
    chosen: chosen ? { source: 'system', httpProxy: '', httpsProxy: '', noProxy: '', results: [], ...chosen } : null,
    tried: [],
  });

  it('reads the saved values, and the draft over them', () => {
    expect(saved).toEqual({ httpProxy: 'http://saved:1', httpsProxy: 'http://saved:1', noProxy: 'intranet' });
    expect(proxyValues(fields, { 'proxy:httpsProxy': 'http://new:2' })).toEqual({ ...saved, httpsProxy: 'http://new:2' });
  });

  it('starts from the working configuration when its values can be shown', () => {
    expect(proxyPrefill(network({ httpProxy: 'http://corp:911', httpsProxy: 'http://corp:911' }), saved)).toEqual({
      httpProxy: 'http://corp:911',
      httpsProxy: 'http://corp:911',
      noProxy: 'intranet',
    });
    expect(proxyPrefill(network({ source: 'environment', httpsProxy: 'http://***@corp:911' }), saved)).toEqual(saved);
    expect(proxyPrefill(network({ source: 'direct' }), saved)).toEqual({ httpProxy: '', httpsProxy: '', noProxy: 'intranet' });
    expect(proxyPrefill(null, saved)).toEqual(saved);
  });

  it('turns new values into draft entries, for what changed only', () => {
    expect(proxyDraft(saved, saved)).toEqual({});
    expect(proxyDraft({ ...saved, httpsProxy: ' http://new:2 ' }, saved)).toEqual({ 'proxy:httpsProxy': 'http://new:2' });
  });
});

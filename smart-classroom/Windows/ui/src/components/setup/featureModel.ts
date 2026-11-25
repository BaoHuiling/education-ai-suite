// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The feature picker's decisions, without React, for tests/renderer. The
// groups themselves come from the schema (FEATURE_GROUPS in config-schema.cjs).

import type { ConfigField, FeatureGroup } from '../../types/config';

export const featurePath = (id: string) => `features.${id}.enabled`;

/** The toggle fields of a group's features, in the group's order. */
export function groupFields(group: FeatureGroup, fields: ConfigField[]): ConfigField[] {
  return group.features
    .map((id) => fields.find((field) => field.file === 'config' && field.path === featurePath(id)))
    .filter((field): field is ConfigField => !!field);
}

/** On, off, or some of its features on. */
export function groupState(members: ConfigField[], isOn: (field: ConfigField) => boolean): 'on' | 'off' | 'mixed' {
  const on = members.filter(isOn).length;
  if (members.length && on === members.length) return 'on';
  return on === 0 ? 'off' : 'mixed';
}

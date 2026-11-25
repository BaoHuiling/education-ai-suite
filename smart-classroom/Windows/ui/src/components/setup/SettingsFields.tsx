// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import '../../assets/css/Config.css';
import type { ConfigField, ConfigProblem, ConfigValue } from '../../types/config';
import { fieldKey, unmetCondition } from '../../services/configManager';
import type { Draft } from '../../services/settingsDraft';
import ConfigFieldControl from '../Settings/ConfigFieldControl';

interface SettingsFieldsProps {
  fields: ConfigField[];
  /** Every field, for the conditions that switch one field off by another's value. */
  allFields: ConfigField[];
  draft: Draft;
  byField: Map<string, ConfigProblem>;
  onChange: (field: ConfigField, value: ConfigValue) => void;
  flashedKey?: string | null;
  /** Prefix of each control's id, for scrolling to a field. */
  idPrefix?: string;
}

/** Settings as form controls: the same on every screen that edits them. */
const SettingsFields: React.FC<SettingsFieldsProps> = ({ fields, allFields, draft, byField, onChange, flashedKey, idPrefix }) => (
  <div className={`config-fields${fields.every((field) => field.type === 'boolean') ? ' compact' : ''}`}>
    {fields.map((field) => (
      <ConfigFieldControl
        key={fieldKey(field)}
        id={idPrefix ? `${idPrefix}${fieldKey(field)}` : undefined}
        field={field}
        draft={draft}
        unmet={unmetCondition(field, allFields, draft)}
        flashed={flashedKey === fieldKey(field)}
        problem={byField.get(fieldKey(field))}
        onChange={(value) => onChange(field, value)}
      />
    ))}
  </div>
);

export default SettingsFields;

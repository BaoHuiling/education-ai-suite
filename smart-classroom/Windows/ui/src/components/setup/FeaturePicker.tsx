// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Features chosen by group, one section each, with its individual toggles
// below: the same in the setup assistant and on the Configuration screen.

import React from 'react';
import { useTranslation } from 'react-i18next';
import '../../assets/css/SettingsSections.css';
import type { ConfigDescription, ConfigField, ConfigProblem, ConfigValue } from '../../types/config';
import { fieldKey } from '../../services/configManager';
import type { Draft } from '../../services/settingsDraft';
import { groupFields, groupState } from './featureModel';
import SettingsFields from './SettingsFields';

interface FeaturePickerProps {
  description: ConfigDescription;
  draft: Draft;
  byField: Map<string, ConfigProblem>;
  onChange: (field: ConfigField, value: ConfigValue) => void;
  /** Also show each group's own settings (fields placed in it), as the assistant does. */
  withSettings?: boolean;
  flashedKey?: string | null;
  idPrefix?: string;
}

const FeaturePicker: React.FC<FeaturePickerProps> = ({
  description,
  draft,
  byField,
  onChange,
  withSettings = false,
  flashedKey,
  idPrefix,
}) => {
  const { t } = useTranslation();
  const fields = description.fields;

  const isOn = (field: ConfigField) => (draft[fieldKey(field)] ?? field.value) !== false;

  return (
    <div className="feature-picker">
      {description.featureGroups.map((group) => {
        const members = groupFields(group, fields);
        const state = groupState(members, isOn);
        const settings = withSettings ? fields.filter((field) => field.onboarding === group.id) : [];
        const title = t(`featureGroups.${group.id}.title`, group.label);
        return (
          <section key={group.id} className={`feature-group ${state}`} aria-labelledby={`feature-group-title-${group.id}`}>
            <div className="feature-group-head">
              <input
                id={`feature-group-${group.id}`}
                type="checkbox"
                checked={state === 'on'}
                ref={(input) => {
                  if (input) input.indeterminate = state === 'mixed';
                }}
                onChange={(event) => members.forEach((field) => onChange(field, event.target.checked))}
              />
              <label htmlFor={`feature-group-${group.id}`} className="feature-group-text">
                <span id={`feature-group-title-${group.id}`} className="feature-group-title">
                  {title}
                </span>
                <span className="feature-group-detail">{t(`featureGroups.${group.id}.detail`, group.detail)}</span>
                {state === 'mixed' && (
                  <span className="feature-group-count">
                    {t('featureGroups.count', '{{on}} of {{total}} on', {
                      on: members.filter(isOn).length,
                      total: members.length,
                    })}
                  </span>
                )}
              </label>
            </div>
            <div className="feature-group-body">
              <SettingsFields
                fields={members}
                allFields={fields}
                draft={draft}
                byField={byField}
                onChange={onChange}
                flashedKey={flashedKey}
                idPrefix={idPrefix}
              />
              {settings.length > 0 && (
                <SettingsFields
                  fields={settings}
                  allFields={fields}
                  draft={draft}
                  byField={byField}
                  onChange={onChange}
                  flashedKey={flashedKey}
                  idPrefix={idPrefix}
                />
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
};

export default FeaturePicker;

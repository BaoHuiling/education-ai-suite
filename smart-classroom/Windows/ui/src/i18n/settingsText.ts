// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// UI-language text for what the Electron main process describes in English:
// settings fields, the cross-field rules, save errors and setup checks.
//
// The main process sends stable ids (a field path, a rule id, an error code, a
// detail key) and the values that go into the sentence; the text is looked up
// here and the English it sent is the fallback. English therefore needs no
// entries in en.json, and a key a translation lacks still shows something.

import i18n, { type TFunction } from 'i18next';
import type { ConfigField, ConfigProblem } from '../types/config';
import type { SetupStep } from '../types/setup';
import { toCode, toMessage, toParams } from '../services/ipcResult';

type FieldText = Pick<ConfigField, 'path' | 'label' | 'help'>;

// configFields nests by the dotted path, so it reads like config.yaml itself.
export const fieldLabel = (t: TFunction, field: FieldText): string =>
  t(`configFields.${field.path}.label`, { defaultValue: field.label });

export const fieldHelp = (t: TFunction, field: FieldText): string | null =>
  field.help ? t(`configFields.${field.path}.help`, { defaultValue: field.help }) : null;

const featureLabel = (t: TFunction, id: string): string =>
  t(`configFields.features.${id}.enabled.label`, { defaultValue: id });

/** "A, B and C" in the UI language; a rule message lists two to six features. */
const listOf = (items: string[]): string =>
  new Intl.ListFormat(i18n.language, { style: 'long', type: 'conjunction' }).format(items);

export function localizeProblem(t: TFunction, problem: ConfigProblem): ConfigProblem {
  const params = Object.fromEntries(
    Object.entries(problem.params ?? {}).map(([key, value]) => [
      key,
      Array.isArray(value) ? listOf(value.map((id) => featureLabel(t, id))) : value,
    ])
  );
  const summaryKey = problem.summaryId
    ? `config.rules.${problem.rule}.summaries.${problem.summaryId}`
    : `config.rules.${problem.rule}.summary`;

  return {
    ...problem,
    summary: t(summaryKey, { defaultValue: problem.summary }),
    message: t(`config.rules.${problem.rule}.message`, { ...params, defaultValue: problem.message }),
  };
}

/** A failed save. Only coerce()'s coded errors are translatable; anything else is shown as sent. */
export function configErrorText(t: TFunction, error: unknown, fields: FieldText[]): string {
  const code = toCode(error);
  const message = toMessage(error);
  if (!code?.startsWith('config-')) return message;

  const params = toParams(error);
  const field = fields.find((entry) => entry.path === params.path);
  return t(`config.errors.${code.slice('config-'.length)}`, {
    ...params,
    field: field ? fieldLabel(t, field) : params.path,
    defaultValue: message,
  });
}

export function localizeStep(t: TFunction, step: SetupStep): SetupStep {
  const params = step.detailParams ?? {};
  return {
    ...step,
    label: t(`setup.steps.${step.id}`, { defaultValue: step.label }),
    detail: step.detailKey
      ? t(`setup.details.${step.detailKey}`, {
          ...params,
          // "Running" names the action, which has its own translation.
          ...(params.action ? { action: t(`setup.actions.${params.action}`, { defaultValue: String(params.action) }) } : {}),
          defaultValue: step.detail,
        })
      : step.detail,
    actions: step.actions.map((action) => ({
      ...action,
      label: t(`setup.actions.${action.id}`, { defaultValue: action.label }),
    })),
  };
}

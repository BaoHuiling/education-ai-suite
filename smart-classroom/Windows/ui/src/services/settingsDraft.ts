// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Pending edits to a set of settings, with the rule problems they would cause.
// The Configuration screen and the setup assistant's pages edit settings
// through this one hook, so they validate, flag and fix alike.

import { useCallback, useMemo, useState } from 'react';
import type { ConfigChange, ConfigDescription, ConfigField, ConfigValue } from '../types/config';
import { fieldKey, NO_CHANGES, useConfigProblems, withFix } from './configManager';

export type Draft = Record<string, ConfigValue>;

export function useSettingsDraft(description: ConfigDescription | null, fields: ConfigField[]) {
  // Only edited fields land here, which is also what keeps untouched secrets
  // out of the payload.
  const [draft, setDraft] = useState<Draft>({});

  // A stable empty value matters: useConfigProblems re-validates whenever this
  // changes identity.
  const changes: ConfigChange[] = useMemo(() => {
    const pending = fields
      .filter((field) => draft[fieldKey(field)] !== undefined)
      .map((field) => ({ file: field.file, path: field.path, value: draft[fieldKey(field)] }));
    return pending.length ? pending : NO_CHANGES;
  }, [fields, draft]);

  const { problems, blocking, byField, messages } = useConfigProblems(changes, description);

  const setValue = useCallback(
    (field: ConfigField, value: ConfigValue) => setDraft((previous) => ({ ...previous, [fieldKey(field)]: value })),
    []
  );

  /** A rule's one-click fix, merged into the draft: it shows in the save count and Discard undoes it. */
  const applyFix = useCallback((fix: ConfigChange[]) => setDraft((previous) => withFix(previous, fix)), []);

  /** A fix may name a field this view does not edit; then only the Configuration screen can apply it. */
  const canApply = useCallback(
    (fix: ConfigChange[]) => fix.every((change) => fields.some((field) => field.file === change.file && field.path === change.path)),
    [fields]
  );

  const reset = useCallback(() => setDraft({}), []);

  return { draft, setDraft, changes, problems, blocking, byField, messages, setValue, applyFix, canApply, reset };
}

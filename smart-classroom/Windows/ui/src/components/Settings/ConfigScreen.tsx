// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Every setting, grouped and searchable: the expert editor behind the setup
// assistant. Features and the proxy use the same sections as the assistant.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import '../../assets/css/Config.css';
import type { ConfigField, ConfigProblem, ConfigSubgroup } from '../../types/config';
import { revealConfig, useConfig, fieldKey as keyOf } from '../../services/configManager';
import { useServices } from '../../services/serviceManager';
import { useSettingsDraft } from '../../services/settingsDraft';
import { fieldHelp, fieldLabel } from '../../i18n/settingsText';
import FeaturePicker from '../setup/FeaturePicker';
import ProblemBanners from '../setup/ProblemBanners';
import ProxySettings from '../setup/ProxySettings';
import SettingsFields from '../setup/SettingsFields';
import { Banner, Button, PageHeader } from '../ui';

/** Fields of one group, split into the subgroup sections they render as. */
interface Section {
  subgroup: ConfigSubgroup | null;
  fields: ConfigField[];
}

interface ConfigScreenProps {
  /** Restarting hands over to Services, which is where the logs and status are. */
  onOpenScreen?: (screen: 'services') => void;
  /** Dotted path of a field to open on and flag, when opened from the assistant. */
  focusPath?: string | null;
}

const NO_FIELDS: ConfigField[] = [];

const ConfigScreen: React.FC<ConfigScreenProps> = ({ onOpenScreen, focusPath }) => {
  const { t } = useTranslation();
  const { description, error, saving, save } = useConfig();
  // serviceError is separate from useConfig's: without it a failed restart is
  // indistinguishable from a dead button.
  const { services, restart, busyId, error: serviceError } = useServices();
  const allFields = description?.fields ?? NO_FIELDS;
  // Built from every field, not the filtered view: an edit the search is
  // currently hiding still has to be saved, and still has to be validated.
  const { draft, changes, blocking, byField, messages, setValue, applyFix, canApply, reset } = useSettingsDraft(
    description,
    allFields
  );
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [flashed, setFlashed] = useState<string | null>(null);
  const consumedFocus = useRef<string | null>(null);

  const backend = services.find((service) => service.id === 'backend');
  const backendRunning = !!backend && backend.status !== 'stopped' && backend.status !== 'failed';
  const dirtyCount = Object.keys(draft).length;

  const groupLabel = (id: string, fallback: string) => t(`config.groups.${id}`, fallback);
  const subgroupLabel = (subgroup: ConfigSubgroup) => t(`config.subgroups.${subgroup.id}`, subgroup.label);

  // Every token has to match somewhere, so "qa tokens" narrows to
  // content_search.qa.max_tokens rather than returning every "tokens" field.
  const tokens = useMemo(() => query.toLowerCase().split(/\s+/).filter(Boolean), [query]);

  // Deliberately not searching the group label: the nav already selects groups,
  // and folding it in means "device" returns every field under "Models and
  // devices" whether or not it names one.
  const matches = useMemo(() => {
    if (!description || !tokens.length) return null;
    const byId = new Map(description.subgroups.map((s) => [s.id, s]));
    const hits = new Set<string>();
    for (const field of description.fields) {
      const subgroup = field.subgroup ? byId.get(field.subgroup) : undefined;
      // English as well as the translation: the help text and docs name
      // settings in English, so either should find them.
      const haystack = [
        field.label,
        fieldLabel(t, field),
        field.path,
        field.help ?? '',
        fieldHelp(t, field) ?? '',
        subgroup ? subgroupLabel(subgroup) : '',
        subgroup?.node ?? '',
      ]
        .join(' ')
        .toLowerCase();
      if (tokens.every((token) => haystack.includes(token))) hits.add(keyOf(field));
    }
    return hits;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [description, tokens, t]);

  const grouped = useMemo(() => {
    if (!description) return [];
    const visible = description.fields.filter((field) => !matches || matches.has(keyOf(field)));

    return description.groups
      .map((group) => {
        const fields = visible.filter((field) => field.group === group.id);
        const sections: Section[] = [];

        const loose = fields.filter((field) => !field.subgroup);
        if (loose.length) sections.push({ subgroup: null, fields: loose });

        for (const subgroup of description.subgroups.filter((s) => s.group === group.id)) {
          const owned = fields.filter((field) => field.subgroup === subgroup.id);
          if (owned.length) sections.push({ subgroup, fields: owned });
        }

        return { group, fields, sections };
      })
      .filter((entry) => entry.fields.length > 0);
  }, [description, matches]);

  // Unsaved edits can sit in a group the user has navigated away from, so
  // surface the count next to each entry. Counted over every field, not just
  // the visible ones, so a filter cannot hide pending work.
  const dirtyByGroup = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const field of allFields) {
      if (draft[keyOf(field)] !== undefined) counts[field.group] = (counts[field.group] ?? 0) + 1;
    }
    return counts;
  }, [allFields, draft]);

  const active = grouped.find((entry) => entry.group.id === selectedGroup) ?? grouped[0];

  // Searching can empty the selected group; follow the results instead of
  // showing a blank panel.
  useEffect(() => {
    if (grouped.length && !grouped.some((entry) => entry.group.id === selectedGroup)) {
      setSelectedGroup(grouped[0].group.id);
    }
  }, [grouped, selectedGroup]);

  const showField = (field: ConfigField) => {
    setQuery('');
    setSelectedGroup(field.group);
    setFlashed(keyOf(field));
    // The group only renders on the next paint, so the node cannot be found yet.
    requestAnimationFrame(() => {
      document.getElementById(`config-field-${keyOf(field)}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    // Matches the flash animation, so showing it again re-runs it.
    setTimeout(() => setFlashed(null), 2200);
  };

  // Arriving from the assistant with one field in mind: switch to its group,
  // drop any filter that would hide it, then scroll to it and flag it. Consumed
  // once: description is replaced after every save, and the screen must not
  // jump back here each time.
  useEffect(() => {
    if (!focusPath || !description || consumedFocus.current === focusPath) return;
    const field = description.fields.find((entry) => entry.path === focusPath);
    if (!field) return;
    consumedFocus.current = focusPath;
    showField(field);
  }, [focusPath, description]);

  const handleSave = async () => {
    if (!changes.length || blocking.length) return;
    if (await save(changes)) {
      reset();
      setSavedAt(Date.now());
    }
  };

  const showSetting = (problem: ConfigProblem) => {
    const field = allFields.find((entry) => entry.file === problem.file && entry.path === problem.path);
    return field ? <Button onClick={() => showField(field)}>{t('config.showSetting', 'Show setting')}</Button> : null;
  };

  const matchCount = matches?.size ?? 0;

  // Features and the proxy have their own sections, the assistant's; a search
  // falls back to plain fields, since it narrows to individual settings.
  const renderPanel = () => {
    if (!active || !description) return null;
    if (!matches && active.group.id === 'features') {
      return (
        <FeaturePicker
          description={description}
          draft={draft}
          byField={byField}
          onChange={setValue}
          flashedKey={flashed}
          idPrefix="config-field-"
        />
      );
    }
    if (!matches && active.group.id === 'proxy') {
      return (
        <ProxySettings
          description={description}
          fields={active.fields}
          draft={draft}
          byField={byField}
          onChange={setValue}
          flashedKey={flashed}
          idPrefix="config-field-"
        />
      );
    }
    return active.sections.map(({ subgroup, fields }) => (
      <section className="config-group" key={subgroup?.id ?? '_'}>
        {subgroup && (
          <div className="config-subgroup">
            <h4 className="config-subgroup-title">{subgroupLabel(subgroup)}</h4>
            <code className="config-subgroup-node">{subgroup.node}</code>
          </div>
        )}
        <SettingsFields
          fields={fields}
          allFields={allFields}
          draft={draft}
          byField={byField}
          onChange={setValue}
          flashedKey={flashed}
          idPrefix="config-field-"
        />
      </section>
    ));
  };

  return (
    <div className="config-screen">
      <div className="config-body">
        <PageHeader
          title={t('config.title', 'Configuration')}
          subtitle={t('config.subtitle', 'Edits are written to config.yaml and .proxy-config.')}
          actions={
            <>
              <div className="config-search">
                <input
                  className="config-input config-search-input"
                  type="text"
                  value={query}
                  placeholder={t('config.search', 'Search settings…')}
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') setQuery('');
                  }}
                />
                {query && (
                  <button className="config-search-clear" aria-label={t('config.searchClear', 'Clear search')} onClick={() => setQuery('')}>
                    ×
                  </button>
                )}
              </div>
              <Button onClick={() => revealConfig().catch(() => undefined)}>{t('config.reveal', 'Show file')}</Button>
              <Button disabled={!dirtyCount || saving} onClick={reset}>
                {t('config.discard', 'Discard')}
              </Button>
              <Button
                variant="primary"
                disabled={!dirtyCount || saving || blocking.length > 0}
                title={blocking.length ? t('config.blockedTitle', 'Fix the problems below first.') : undefined}
                onClick={handleSave}
              >
                {saving ? t('config.saving', 'Saving…') : t('config.save', 'Save {{count}} change', { count: dirtyCount })}
              </Button>
            </>
          }
        />

        {error && <Banner tone="error">{error}</Banner>}
        {serviceError && <Banner tone="error">{serviceError}</Banner>}

        <ProblemBanners messages={messages} canApply={canApply} onApplyFix={applyFix} extraActions={showSetting} />

        {savedAt && !dirtyCount && (
          <Banner
            tone={backendRunning ? 'warning' : 'success'}
            actions={
              backendRunning && (
                <Button
                  variant="primary"
                  disabled={busyId === 'backend' || backend?.external}
                  onClick={() => {
                    void restart('backend');
                    setSavedAt(null);
                    onOpenScreen?.('services');
                  }}
                >
                  {t('config.restartBackend', 'Restart backend')}
                </Button>
              )
            }
          >
            {backendRunning
              ? t('config.restartRequired', 'Saved. Restart the backend to apply changes.')
              : t('config.savedIdle', 'Saved. It takes effect the next time the backend starts.')}
          </Banner>
        )}

        {backend?.external && savedAt && (
          <p className="ui-note">
            {t('config.externalBackend', 'The backend was started outside this app, so it cannot be restarted from here.')}
          </p>
        )}

        {!description && !error && <p className="ui-note">{t('config.loading', 'Loading configuration…')}</p>}

        {matches && (
          <p className="ui-note">
            {matchCount
              ? t('config.matchCount', '{{count}} setting matches', { count: matchCount })
              : t('config.noResults', 'No setting matches “{{query}}”.', { query })}
          </p>
        )}

        <div className="config-split">
          <nav className="config-nav">
            {grouped.map(({ group, fields }) => (
              <button
                key={group.id}
                className={`config-nav-item${active?.group.id === group.id ? ' active' : ''}`}
                onClick={() => setSelectedGroup(group.id)}
              >
                <span className="config-nav-label">{groupLabel(group.id, group.label)}</span>
                {dirtyByGroup[group.id] ? (
                  <span className="config-nav-dirty">{dirtyByGroup[group.id]}</span>
                ) : (
                  <span className="config-nav-count">{fields.length}</span>
                )}
              </button>
            ))}
          </nav>

          <div className="config-panel">{renderPanel()}</div>
        </div>
      </div>
    </div>
  );
};

export default ConfigScreen;

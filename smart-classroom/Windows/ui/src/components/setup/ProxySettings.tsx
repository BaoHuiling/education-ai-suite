// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The proxy settings and a test of what they reach: the same on the
// Configuration screen and the setup assistant's Network page.

import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import '../../assets/css/SettingsSections.css';
import type { ConfigDescription, ConfigField, ConfigProblem, ConfigValue } from '../../types/config';
import type { HostResult, NetworkProbeResult, ProxyValues } from '../../types/firstRun';
import { probeNetwork } from '../../services/firstRunManager';
import { toMessage } from '../../services/ipcResult';
import type { Draft } from '../../services/settingsDraft';
import { proxyValues } from './proxyModel';
import { Banner, Button, Row, RowList, Section } from '../ui';
import { causeText, hostGroupText } from './networkText';
import SettingsFields from './SettingsFields';

const PROXY_KEYS: (keyof ProxyValues)[] = ['httpProxy', 'httpsProxy', 'noProxy'];

interface ProxySettingsProps {
  description: ConfigDescription;
  /** The fields to show: the proxy group, or the assistant's network fields. */
  fields: ConfigField[];
  draft: Draft;
  byField: Map<string, ConfigProblem>;
  onChange: (field: ConfigField, value: ConfigValue) => void;
  /** Results already known, such as the first-run test's. Shown until the values change. */
  initialResults?: HostResult[];
  onTested?: (result: NetworkProbeResult) => void;
  flashedKey?: string | null;
  idPrefix?: string;
}

const ProxySettings: React.FC<ProxySettingsProps> = ({
  description,
  fields,
  draft,
  byField,
  onChange,
  initialResults,
  onTested,
  flashedKey,
  idPrefix,
}) => {
  const { t } = useTranslation();
  const values = proxyValues(description.fields, draft);
  const [tested, setTested] = useState<NetworkProbeResult | null>(null);
  const [stale, setStale] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState('');

  // Results describe the values they were made with; edits make them stale.
  const valuesKey = PROXY_KEYS.map((key) => values[key]).join('\n');
  const firstKey = useRef(valuesKey);
  useEffect(() => {
    if (valuesKey === firstKey.current) return;
    setTested(null);
    setStale(true);
  }, [valuesKey]);

  const results = tested?.results ?? (stale ? [] : initialResults ?? []);

  const runTest = async () => {
    setTesting(true);
    setError('');
    try {
      const result = await probeNetwork(values);
      setTested(result);
      onTested?.(result);
    } catch (e) {
      setError(toMessage(e));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="proxy-settings">
      <SettingsFields
        fields={fields}
        allFields={description.fields}
        draft={draft}
        byField={byField}
        onChange={onChange}
        flashedKey={flashedKey}
        idPrefix={idPrefix}
      />
      <p className="ui-note">
        {t(
          'network.proxyNote',
          'Python downloads use the HTTPS proxy. Leave both proxies empty for a direct connection, and include user:password@ in the address if the proxy asks for one.'
        )}
      </p>

      <Section title={t('network.connection', 'Connection')}>
        <div className="proxy-test">
          <span className="ui-note">
            {t('network.testNote', 'Checks that the download hosts answer through these settings, before you save them.')}
          </span>
          <Button disabled={testing} onClick={runTest}>
            {testing ? t('network.testing', 'Testing…') : t('network.test', 'Test connection')}
          </Button>
        </div>
        {error && <Banner tone="error">{error}</Banner>}
        {results.length > 0 && (
          <RowList label={t('network.connection', 'Connection')}>
            {results.map((result) => (
              <Row
                key={result.host}
                state={result.ok ? 'ok' : 'failed'}
                title={result.host}
                meta={result.ok ? `${result.ms} ms` : undefined}
                detail={
                  <>
                    <span>{hostGroupText(t, result.group)}</span>
                    {!result.ok && <span className="ui-row-cause">{causeText(t, result.cause)}</span>}
                  </>
                }
              />
            ))}
          </RowList>
        )}
      </Section>
    </div>
  );
};

export default ProxySettings;

// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import '../../assets/css/Services.css';
import type { ServiceSnapshot } from '../../types/services';
import { runningDownload } from '../../services/downloadProgress';
import { revealLogs, useServiceLogs, useServices } from '../../services/serviceManager';
import { Banner, Button, DownloadBar, LogViewer, PageHeader, Row, RowList, type StatusState } from '../ui';

const STATUS_LABELS: Record<string, string> = {
  stopped: 'services.status.stopped',
  starting: 'services.status.starting',
  healthy: 'services.status.healthy',
  degraded: 'services.status.degraded',
  stopping: 'services.status.stopping',
  failed: 'services.status.failed',
};

// The shared marks for a service's state; stopping counts as in progress.
const STATE: Record<string, StatusState> = {
  stopped: 'pending',
  starting: 'running',
  healthy: 'ok',
  degraded: 'warn',
  stopping: 'running',
  failed: 'failed',
};

function formatUptime(ms: number | null): string {
  if (!ms || ms < 0) return '—';
  const seconds = Math.floor(ms / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

interface TreeNode {
  service: ServiceSnapshot;
  depth: number;
}

/** Depth-first flatten of the ownedBy graph, so children render under their owner. */
function buildTree(services: ServiceSnapshot[]): TreeNode[] {
  const present = new Set(services.map((service) => service.id));
  const nodes: TreeNode[] = [];
  const walk = (parentId: string | null, depth: number) => {
    for (const service of services) {
      const owner = service.ownedBy && present.has(service.ownedBy) ? service.ownedBy : null;
      if (owner !== parentId) continue;
      nodes.push({ service, depth });
      walk(service.id, depth + 1);
    }
  };
  walk(null, 0);
  return nodes;
}

interface ServicesScreenProps {
  /** Setup, where what keeps the backend from starting is fixed. */
  onOpenSetup?: () => void;
}

const ServicesScreen: React.FC<ServicesScreenProps> = ({ onOpenSetup }) => {
  const { t } = useTranslation();
  const openSetup = onOpenSetup && <Button onClick={onOpenSetup}>{t('services.openSetup', 'Open Setup')}</Button>;
  const { services, error, busyId, start, stop, restart } = useServices();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Held here rather than inside LogViewer, which is now shared with Setup.
  const { lines: logLines, clear: clearLogLines } = useServiceLogs(selectedId);
  // Uptime is derived from a timestamp, so tick locally instead of asking the
  // main process to push a snapshot every second.
  const [, setTick] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(interval);
  }, []);

  const visible = useMemo(() => services.filter((service) => service.enabled), [services]);
  const disabled = useMemo(() => services.filter((service) => !service.enabled), [services]);
  const tree = useMemo(() => buildTree(visible), [visible]);
  const controllable = useMemo(() => visible.filter((service) => service.managed), [visible]);

  useEffect(() => {
    if (!selectedId && visible.length) setSelectedId(controllable[0]?.id ?? visible[0].id);
  }, [visible, controllable, selectedId]);

  const renderControls = (service: ServiceSnapshot) => {
    const busy = busyId === service.id;
    const running = service.status !== 'stopped' && service.status !== 'failed';
    const blocked = !!service.startBlockedBy;
    return (
      <div className="services-actions" key={service.id}>
        {controllable.length > 1 && <span className="services-actions-label">{service.label}</span>}
        <Button disabled={busy || running || !service.runnable || blocked} onClick={() => start(service.id)}>
          {t('services.start', 'Start')}
        </Button>
        <Button disabled={busy || !running} onClick={() => stop(service.id)}>
          {t('services.stop', 'Stop')}
        </Button>
        <Button disabled={busy || service.external || !service.runnable || blocked} onClick={() => restart(service.id)}>
          {t('services.restart', 'Restart')}
        </Button>
      </div>
    );
  };

  return (
    <div className="services-screen">
      <PageHeader
        title={t('services.title', 'Services')}
        subtitle={t('services.subtitle', 'Start, stop and inspect the backend processes.')}
        actions={controllable.map(renderControls)}
      />

      {error && <Banner tone="error">{error}</Banner>}

      {/* Setup first: an environment being created is "not runnable" until
          its interpreter exists, and "missing" would tell the user to create
          what is already being created. */}
      {controllable.some((service) => service.startBlockedBy === 'setup') ? (
        <Banner actions={openSetup}>
          {t('services.setupRunning', 'Setup is preparing the Python environment. Start the backend when it finishes.')}
        </Banner>
      ) : (
        controllable.some((service) => !service.runnable) && (
          <Banner actions={openSetup}>
            {t('services.needsSetup', 'The Python environment is missing, so the backend cannot start yet.')}
          </Banner>
        )
      )}

      <div className="services-split">
        <div className="services-pane">
          <div className="services-list">
            <RowList label={t('services.title', 'Services')}>
              {tree.map(({ service, depth }) => {
                const download = runningDownload(service.download);
                return (
                  <Row
                    key={service.id}
                    state={STATE[service.status] ?? 'pending'}
                    title={
                      <span style={{ paddingLeft: depth * 14 }}>
                        {depth > 0 && <span className="services-tree-branch">└ </span>}
                        {service.label}
                      </span>
                    }
                    badge={service.external ? t('services.external', 'external') : undefined}
                    meta={t(STATUS_LABELS[service.status] ?? service.status, service.status)}
                    detail={
                      <>
                        <span className="services-row-meta" style={{ paddingLeft: depth * 14 }}>
                          {[
                            service.port ? `:${service.port}` : null,
                            service.pid ? `pid ${service.pid}` : null,
                            service.uptimeMs ? formatUptime(service.uptimeMs) : null,
                          ]
                            .filter(Boolean)
                            .join('  ·  ') || '—'}
                        </span>
                        {download && (
                          <div style={{ paddingLeft: depth * 14 }}>
                            <DownloadBar progress={download} named />
                          </div>
                        )}
                      </>
                    }
                    selected={selectedId === service.id}
                    onSelect={() => setSelectedId(service.id)}
                  />
                );
              })}
            </RowList>
          </div>

          {visible.some((service) => service.error) && (
            <Banner tone="error">
              {visible
                .filter((service) => service.error)
                .map((service) => `${service.label}: ${service.error}`)
                .join(' · ')}
            </Banner>
          )}

          {disabled.length > 0 && (
            <div className="services-disabled-note">
              {t('services.disabledIn', 'Disabled in config.yaml')}:{' '}
              {disabled.map((service) => service.label).join(', ')}
            </div>
          )}
        </div>

        <LogViewer
          lines={logLines}
          title={visible.find((service) => service.id === selectedId)?.label ?? ''}
          onClear={selectedId ? clearLogLines : undefined}
          onReveal={selectedId ? () => revealLogs(selectedId).catch(() => undefined) : undefined}
        />
      </div>
    </div>
  );
};

export default ServicesScreen;

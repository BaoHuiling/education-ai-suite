// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// How network test results read, for the setup assistant's Network page and
// the Configuration screen's connection test alike.

import type { TFunction } from 'i18next';
import type { HostGroup, NetworkCause, ProxyCandidate } from '../../types/firstRun';

/** How the downloads connect, in a sentence. */
export function connectionText(t: TFunction, candidate: ProxyCandidate | null | undefined): string {
  if (!candidate) return t('network.noConnection', 'Cannot reach the download hosts');
  const proxy = candidate.httpsProxy || candidate.httpProxy;
  if (!proxy) return t('network.direct', 'Direct connection');
  switch (candidate.source) {
    case 'saved':
      return t('network.viaSaved', 'Through the saved proxy {{proxy}}', { proxy });
    case 'environment':
      return t('network.viaEnvironment', 'Through the proxy in the HTTPS_PROXY variable, {{proxy}}', { proxy });
    case 'system':
      return t('network.viaSystem', 'Through the Windows proxy {{proxy}}', { proxy });
    default:
      return t('network.viaEntered', 'Through {{proxy}}', { proxy });
  }
}

export function causeText(t: TFunction, cause: NetworkCause | null): string {
  switch (cause) {
    case 'dns':
      return t('network.causes.dns', 'Host name not found. Check the network connection or the proxy address.');
    case 'timeout':
      return t('network.causes.timeout', 'No answer in time. A proxy may be required.');
    case 'connection':
      return t('network.causes.connection', 'The connection was refused or dropped.');
    case 'proxyUnreachable':
      return t('network.causes.proxyUnreachable', 'The proxy cannot be reached. Check its address and port.');
    case 'proxyAuth':
      return t('network.causes.proxyAuth', 'The proxy needs a user name and password: http://user:password@host:port.');
    case 'proxyRefused':
      return t('network.causes.proxyRefused', 'The proxy refused the connection to this host.');
    case 'certificate':
      return t(
        'network.causes.certificate',
        'The connection is inspected, and its certificate is not trusted. Downloads may fail.'
      );
    case 'unsupportedProxy':
      return t('network.causes.unsupportedProxy', 'This kind of proxy is not supported. Use an http:// proxy.');
    default:
      return t('network.causes.other', 'The connection failed.');
  }
}

export function hostGroupText(t: TFunction, group: HostGroup): string {
  switch (group) {
    case 'packages':
      return t('network.hosts.packages', 'Python packages');
    case 'huggingface':
      return t('network.hosts.huggingface', 'Models from Hugging Face');
    case 'modelscope':
      return t('network.hosts.modelscope', 'Models from ModelScope');
    default:
      return t('network.hosts.installers', 'Installers');
  }
}

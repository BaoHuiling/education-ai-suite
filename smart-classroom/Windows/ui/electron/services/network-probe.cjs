// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Can this machine download what setup needs, and through which proxy?
//
// The test connects the way pip and the model downloads do: an HTTP CONNECT
// tunnel through the proxy, with basic credentials when the proxy URL carries
// them, then a TLS handshake checked against Node's bundled certificate
// authorities, which are close to the certifi bundle Python's downloaders use.
// A network that inspects HTTPS therefore fails here with a certificate error,
// as it would there, instead of passing because a browser trusts the Windows
// certificate store.
//
// Nothing here writes configuration. recommendedChanges() only says what to
// write; first-run.cjs writes it through config-store, with its validation.

const fs = require('fs');
const http = require('http');
const net = require('net');
const tls = require('tls');

const paths = require('./paths.cjs');
const config = require('./config-store.cjs');
const schema = require('./config-schema.cjs');

const TIMEOUT_MS = 10000;

// Initial list, to be confirmed with a traffic capture (first-run wizard
// design, Section 7.3). `group` is what a host is needed for.
const HOSTS = [
  { host: 'pypi.org', group: 'packages' },
  { host: 'files.pythonhosted.org', group: 'packages' },
  { host: 'huggingface.co', group: 'huggingface' },
  { host: 'www.modelscope.cn', group: 'modelscope' },
  { host: 'cdn.winget.microsoft.com', group: 'installers' },
  { host: 'www.python.org', group: 'installers' },
  { host: 'github.com', group: 'installers' },
];

const HUBS = ['huggingface', 'modelscope'];

// Setup steps whose repair downloads an installer rather than packages.
const INSTALLER_STEPS = ['python', 'ffmpeg', 'dlStreamer'];

// Written with a detected proxy when there is no NO_PROXY yet: the backend
// calls its own services on the loopback address, and those must not go
// through the proxy.
const DEFAULT_NO_PROXY = 'localhost,127.0.0.1';

// Why a host could not be reached. The renderer turns these into messages in
// the UI language; they are also what the design doc's table lists.
const CAUSE = {
  DNS: 'dns',
  TIMEOUT: 'timeout',
  CONNECTION: 'connection',
  PROXY_UNREACHABLE: 'proxyUnreachable',
  PROXY_AUTH: 'proxyAuth',
  PROXY_REFUSED: 'proxyRefused',
  CERTIFICATE: 'certificate',
  UNSUPPORTED_PROXY: 'unsupportedProxy',
  OTHER: 'other',
};

const CERTIFICATE_CODES = new Set([
  'CERT_HAS_EXPIRED',
  'CERT_NOT_YET_VALID',
  'CERT_REJECTED',
  'CERT_SIGNATURE_FAILURE',
  'CERT_UNTRUSTED',
  'DEPTH_ZERO_SELF_SIGNED_CERT',
  'ERR_TLS_CERT_ALTNAME_INVALID',
  'SELF_SIGNED_CERT_IN_CHAIN',
  'UNABLE_TO_GET_ISSUER_CERT',
  'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
]);

const CONNECTION_CODES = new Set([
  'ECONNABORTED',
  'ECONNREFUSED',
  'ECONNRESET',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'EPIPE',
]);

/**
 * The cause of a failed connection, from the error and how far it got:
 * `proxy` while connecting to the proxy, `tunnel` while waiting for the proxy
 * to open the tunnel, `connect` and `tls` for the host itself.
 */
function classify(error, phase) {
  const code = error?.code ?? '';
  // Wherever it happens: an inspecting proxy presents its own certificate.
  if (CERTIFICATE_CODES.has(code)) return CAUSE.CERTIFICATE;
  if (phase === 'proxy') return CAUSE.PROXY_UNREACHABLE;
  if (phase === 'tunnel') return code === 'ETIMEDOUT' ? CAUSE.TIMEOUT : CAUSE.PROXY_REFUSED;
  if (code === 'ENOTFOUND' || code === 'EAI_AGAIN') return CAUSE.DNS;
  if (code === 'ETIMEDOUT') return CAUSE.TIMEOUT;
  if (CONNECTION_CODES.has(code)) return CAUSE.CONNECTION;
  return CAUSE.OTHER;
}

/** { protocol, host, port, auth } for an http(s) proxy URL, or null. */
function parseProxy(url) {
  if (!url) return null;
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const secure = parsed.protocol === 'https:';
  return {
    protocol: parsed.protocol,
    host: parsed.hostname.replace(/^\[|\]$/g, ''),
    port: Number(parsed.port) || (secure ? 443 : 80),
    auth: parsed.username
      ? `${decodeURIComponent(parsed.username)}:${decodeURIComponent(parsed.password)}`
      : null,
  };
}

/** A proxy URL with its credentials hidden, for anything shown or logged. */
function maskProxy(url) {
  return String(url ?? '').replace(/\/\/[^@/]+@/, '//***@');
}

/** Whether NO_PROXY sends `host` direct, by the suffix rules pip and requests use. */
function bypassesProxy(host, noProxy) {
  const target = host.toLowerCase();
  return String(noProxy ?? '')
    .split(',')
    .map((entry) => entry.trim().toLowerCase().replace(/:\d+$/, ''))
    .filter(Boolean)
    .some((entry) => {
      if (entry === '*') return true;
      const domain = entry.replace(/^\*?\./, '');
      return target === domain || target.endsWith(`.${domain}`);
    });
}

/**
 * Open a TLS connection to `host`, direct or through `proxy`, and report
 * whether it worked and, if not, why. Never rejects.
 */
function probeHost({ host, port = 443 }, proxy, { timeoutMs = TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const disposers = [];
    let phase = proxy ? 'proxy' : 'connect';
    let settled = false;

    const finish = (ok, cause = null, detail = '') => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // Last opened, first destroyed: the TLS socket goes before the tunnel
      // it runs over. Destroying a socket out from under a live TLS layer
      // crashes Node (seen as a segmentation fault on Node 25).
      for (const dispose of disposers.reverse()) dispose();
      resolve({ ok, cause, detail, ms: Date.now() - started });
    };
    // `on`, not `once`: a socket can report a second error while being torn
    // down, and an error with no listener would take the process down.
    const fail = (error) => finish(false, classify(error, phase), error?.code || error?.message || '');
    const track = (emitter) => {
      emitter.on('error', fail);
      disposers.push(() => emitter.destroy());
      return emitter;
    };
    const timer = setTimeout(
      () => finish(false, phase === 'proxy' ? CAUSE.PROXY_UNREACHABLE : CAUSE.TIMEOUT, 'timeout'),
      timeoutMs
    );

    // An IP address is not a valid SNI name, and Node warns when given one.
    const servername = net.isIP(host) ? undefined : host;

    if (!proxy) {
      const socket = track(tls.connect({ host, port, servername }));
      socket.once('connect', () => {
        phase = 'tls';
      });
      socket.once('secureConnect', () => finish(true));
      return;
    }

    if (proxy.protocol !== 'http:' && proxy.protocol !== 'https:') {
      finish(false, CAUSE.UNSUPPORTED_PROXY, proxy.protocol);
      return;
    }

    const headers = { Host: `${host}:${port}` };
    if (proxy.auth) headers['Proxy-Authorization'] = `Basic ${Buffer.from(proxy.auth).toString('base64')}`;
    const secureProxy = proxy.protocol === 'https:';

    const request = track(
      http.request({
        host: proxy.host,
        port: proxy.port,
        method: 'CONNECT',
        path: `${host}:${port}`,
        headers,
        agent: false,
        ...(secureProxy
          ? {
              createConnection: () =>
                tls.connect({ host: proxy.host, port: proxy.port, servername: net.isIP(proxy.host) ? undefined : proxy.host }),
            }
          : {}),
      })
    );

    // Connected to the proxy: from here a failure is the tunnel's.
    request.once('socket', (socket) => {
      const tunnel = () => {
        phase = 'tunnel';
      };
      if (secureProxy) socket.once('secureConnect', tunnel);
      else if (socket.connecting) socket.once('connect', tunnel);
      else tunnel();
    });

    request.once('connect', (response, socket) => {
      track(socket);
      if (response.statusCode === 407) return finish(false, CAUSE.PROXY_AUTH, '407');
      if (response.statusCode !== 200) return finish(false, CAUSE.PROXY_REFUSED, String(response.statusCode));
      phase = 'tls';
      const secured = track(tls.connect({ socket, servername }));
      secured.once('secureConnect', () => finish(true));
      return undefined;
    });

    // Node reports a reply to CONNECT through 'connect' whatever its status;
    // this covers anything that arrives as an ordinary response instead.
    request.once('response', (response) => {
      response.resume();
      finish(false, response.statusCode === 407 ? CAUSE.PROXY_AUTH : CAUSE.PROXY_REFUSED, String(response.statusCode));
    });

    request.end();
  });
}

/**
 * Probe every host for one proxy configuration. HTTPS targets use the
 * HTTPS proxy only: like pip and requests, there is no fallback to the HTTP
 * one, so a configuration with only HTTP_PROXY goes direct for these hosts.
 */
async function probeCandidate(candidate, hosts = HOSTS, options = {}) {
  const proxy = candidate.httpsProxy ? parseProxy(candidate.httpsProxy) : null;
  if (candidate.httpsProxy && !proxy) {
    return hosts.map((target) => ({ ...target, ok: false, cause: CAUSE.UNSUPPORTED_PROXY, detail: 'invalid URL', ms: 0 }));
  }
  return Promise.all(
    hosts.map(async (target) => {
      const via = proxy && !bypassesProxy(target.host, candidate.noProxy) ? proxy : null;
      return { ...target, ...(await probeHost(target, via, options)) };
    })
  );
}

/** First usable entry of a PAC-style answer from session.resolveProxy(). */
function parseSystemRule(rule) {
  for (const entry of String(rule ?? '').split(';')) {
    const [type, address] = entry.trim().split(/\s+/);
    if (!type) continue;
    if (/^(PROXY|HTTP)$/i.test(type) && address) return `http://${address}`;
    if (/^HTTPS$/i.test(type) && address) return `https://${address}`;
    if (/^DIRECT$/i.test(type)) return '';
    // SOCKS: pip needs an extra package for it, so it is not offered.
  }
  return null;
}

/**
 * The proxy configurations worth trying, most specific first: the saved
 * .proxy-config, the environment variables, the Windows system proxy, and a
 * direct connection. Duplicates collapse into the first.
 */
function candidates({ saved = null, env = null, system = null } = {}) {
  const list = [];
  const add = (source, httpProxy, httpsProxy, noProxy) => {
    if (list.some((entry) => entry.httpProxy === httpProxy && entry.httpsProxy === httpsProxy)) return;
    list.push({ source, httpProxy, httpsProxy, noProxy });
  };
  // A saved file with no proxy is a deliberate choice of a direct connection.
  if (saved) add('saved', saved.httpProxy, saved.httpsProxy, saved.noProxy);
  if (env && (env.httpProxy || env.httpsProxy)) add('environment', env.httpProxy, env.httpsProxy, env.noProxy);
  if (system) add('system', system, system, '');
  add('direct', '', '', '');
  return list;
}

const reachable = (results, group) => {
  const members = results.filter((result) => result.group === group);
  return members.length > 0 && members.every((result) => result.ok);
};

/**
 * Try the candidates in order and stop at the first that reaches the package
 * hosts, which every installation needs. Resolves to the saved configuration
 * (null when there is no .proxy-config), the chosen candidate (null when none
 * worked) and every candidate tried with its results.
 */
async function test({ saved = null, env = null, resolveSystem = null, hosts = HOSTS, probe = probeCandidate } = {}) {
  let system = null;
  if (resolveSystem) {
    try {
      system = parseSystemRule(await resolveSystem('https://pypi.org'));
    } catch {
      system = null;
    }
  }
  const tried = [];
  for (const candidate of candidates({ saved, env, system })) {
    const entry = { ...candidate, results: await probe(candidate, hosts) };
    tried.push(entry);
    if (reachable(entry.results, 'packages')) return { saved, chosen: entry, tried };
  }
  return { saved, chosen: null, tried };
}

/**
 * Whether the recommended settings will work: the package hosts, a model hub,
 * and — when setup still has to download Python, FFmpeg or DL Streamer — the
 * installer hosts. When the configured hub is unreachable and the other works,
 * the other is suggested, and applying the suggestion is what makes it work.
 */
function evaluate(results, { hub = 'huggingface', needInstallers = false } = {}) {
  const hubs = Object.fromEntries(HUBS.map((id) => [id, reachable(results, id)]));
  const other = HUBS.find((id) => id !== hub);
  const suggestedHub = !hubs[hub] && hubs[other] ? other : null;
  const packages = reachable(results, 'packages');
  const installers = reachable(results, 'installers');
  return {
    ok: packages && (hubs[hub] || !!suggestedHub) && (!needInstallers || installers),
    packages,
    installers,
    needInstallers,
    hubs,
    hub,
    suggestedHub,
  };
}

/** Whether a setup plan still has to download an installer. */
const needsInstallers = (plan) => plan.some((entry) => INSTALLER_STEPS.includes(entry.stepId));

/**
 * The configuration changes that make the recommended path use what the test
 * found: the working proxy, when it is not already the saved one, and the
 * suggested model hub. In config-store's change format.
 */
function recommendedChanges({ saved, chosen }, evaluation) {
  const changes = [];
  const proxyChange = (key, value) => ({ file: schema.PROXY, path: key, value });

  if (chosen && chosen.source !== 'saved') {
    const usesProxy = !!(chosen.httpProxy || chosen.httpsProxy);
    const differs = !saved || saved.httpProxy !== chosen.httpProxy || saved.httpsProxy !== chosen.httpsProxy;
    // A direct connection with nothing saved needs no file at all. A saved
    // proxy that failed where direct worked has to be cleared, or every
    // download would keep trying it.
    if (differs && (usesProxy || saved)) {
      changes.push(
        proxyChange('httpProxy', chosen.httpProxy),
        proxyChange('httpsProxy', chosen.httpsProxy),
        proxyChange('noProxy', usesProxy ? chosen.noProxy || saved?.noProxy || DEFAULT_NO_PROXY : saved?.noProxy ?? '')
      );
    }
  }
  if (evaluation?.suggestedHub) {
    changes.push({ file: schema.CONFIG, path: 'models.model_hub', value: evaluation.suggestedHub });
  }
  return changes;
}

/** The saved .proxy-config, or null when there is none. */
function readSaved() {
  return fs.existsSync(paths.proxyConfigFile()) ? config.readProxyConfig() : null;
}

/** The proxy variables of an environment, either case, as a candidate. */
function readEnv(env = process.env) {
  return {
    httpProxy: env.HTTP_PROXY || env.http_proxy || '',
    httpsProxy: env.HTTPS_PROXY || env.https_proxy || '',
    noProxy: env.NO_PROXY || env.no_proxy || '',
  };
}

/**
 * Proxy values typed by a user, checked by the same rules as the
 * Configuration screen. Throws with a message safe to show.
 */
function validateEntered(values) {
  const field = (key) => schema.get(schema.PROXY, key);
  return {
    source: 'entered',
    httpProxy: schema.coerce(field('httpProxy'), values?.httpProxy ?? ''),
    httpsProxy: schema.coerce(field('httpsProxy'), values?.httpsProxy ?? ''),
    noProxy: schema.coerce(field('noProxy'), values?.noProxy ?? ''),
  };
}

/** A candidate as the renderer may see it: credentials hidden. */
function masked(candidate) {
  if (!candidate) return null;
  return {
    ...candidate,
    httpProxy: maskProxy(candidate.httpProxy),
    httpsProxy: maskProxy(candidate.httpsProxy),
  };
}

/**
 * The Network page's test of values the user typed. Rejects with the
 * validation message before connecting anywhere.
 */
async function probeEntered(values, { hub = 'huggingface', needInstallers = false, probe = probeCandidate } = {}) {
  const candidate = validateEntered(values);
  const results = await probe(candidate, HOSTS);
  return { candidate: masked(candidate), results, evaluation: evaluate(results, { hub, needInstallers }) };
}

module.exports = {
  CAUSE,
  HOSTS,
  DEFAULT_NO_PROXY,
  classify,
  parseProxy,
  maskProxy,
  bypassesProxy,
  probeHost,
  probeCandidate,
  parseSystemRule,
  candidates,
  test,
  evaluate,
  needsInstallers,
  recommendedChanges,
  readSaved,
  readEnv,
  validateEntered,
  masked,
  probeEntered,
};

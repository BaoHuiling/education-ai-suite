// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// The first-run network test. The pure parts are tested directly; probeHost()
// is tested against fake proxies on the loopback interface, so nothing here
// leaves the machine and no certificate is needed.

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('http');
const net = require('net');

const probe = require('../../electron/services/network-probe.cjs');

const { CAUSE } = probe;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A CONNECT proxy on 127.0.0.1 whose behaviour `onConnect` decides. */
function fakeProxy(t, onConnect) {
  const server = http.createServer((_request, response) => {
    response.writeHead(405);
    response.end();
  });
  const sockets = new Set();
  server.on('connection', (socket) => sockets.add(socket));
  server.on('connect', (request, socket) => {
    sockets.add(socket);
    onConnect(request, socket);
  });
  t.after(() => {
    for (const socket of sockets) socket.destroy();
    server.close();
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

/** A port nothing listens on. */
function closedPort() {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

const proxyAt = (port, auth = '') => probe.parseProxy(`http://${auth}127.0.0.1:${port}`);
const TARGET = { host: 'pypi.org', port: 443 };

const result = (host, group, ok, cause = null) => ({ host, group, ok, cause, detail: '', ms: 1 });
const allOk = probe.HOSTS.map((entry) => result(entry.host, entry.group, true));
const withFailing = (groups) =>
  probe.HOSTS.map((entry) => result(entry.host, entry.group, !groups.includes(entry.group), groups.includes(entry.group) ? CAUSE.TIMEOUT : null));

// ---------------------------------------------------------------------------
// Classification and parsing
// ---------------------------------------------------------------------------

test('errors are classified by code and by how far the connection got', () => {
  const error = (code) => Object.assign(new Error(code), { code });
  assert.equal(probe.classify(error('ENOTFOUND'), 'connect'), CAUSE.DNS);
  assert.equal(probe.classify(error('EAI_AGAIN'), 'connect'), CAUSE.DNS);
  assert.equal(probe.classify(error('ETIMEDOUT'), 'connect'), CAUSE.TIMEOUT);
  assert.equal(probe.classify(error('ECONNREFUSED'), 'connect'), CAUSE.CONNECTION);
  assert.equal(probe.classify(error('ECONNRESET'), 'tls'), CAUSE.CONNECTION);
  assert.equal(probe.classify(error('UNABLE_TO_GET_ISSUER_CERT_LOCALLY'), 'tls'), CAUSE.CERTIFICATE);
  assert.equal(probe.classify(error('SELF_SIGNED_CERT_IN_CHAIN'), 'tls'), CAUSE.CERTIFICATE);
  // Reaching the proxy is the proxy's problem, whatever the code.
  assert.equal(probe.classify(error('ENOTFOUND'), 'proxy'), CAUSE.PROXY_UNREACHABLE);
  assert.equal(probe.classify(error('ECONNREFUSED'), 'proxy'), CAUSE.PROXY_UNREACHABLE);
  // An inspecting proxy presents its own certificate.
  assert.equal(probe.classify(error('CERT_HAS_EXPIRED'), 'proxy'), CAUSE.CERTIFICATE);
  assert.equal(probe.classify(error('ECONNRESET'), 'tunnel'), CAUSE.PROXY_REFUSED);
  assert.equal(probe.classify(error('ETIMEDOUT'), 'tunnel'), CAUSE.TIMEOUT);
  assert.equal(probe.classify(error('EWHATEVER'), 'tls'), CAUSE.OTHER);
});

test('proxy URLs are parsed with their credentials, and masked for display', () => {
  assert.deepEqual(probe.parseProxy('http://user:p%40ss@proxy.example.com:911'), {
    protocol: 'http:',
    host: 'proxy.example.com',
    port: 911,
    auth: 'user:p@ss',
  });
  assert.equal(probe.parseProxy('http://proxy.example.com').port, 80);
  assert.equal(probe.parseProxy('https://proxy.example.com').port, 443);
  assert.equal(probe.parseProxy('not a url'), null);
  assert.equal(probe.parseProxy(''), null);

  assert.equal(probe.maskProxy('http://user:secret@proxy:911'), 'http://***@proxy:911');
  assert.equal(probe.maskProxy('http://proxy:911'), 'http://proxy:911');
});

test('NO_PROXY follows the suffix rules pip and requests use', () => {
  assert.equal(probe.bypassesProxy('pypi.org', 'localhost,pypi.org'), true);
  assert.equal(probe.bypassesProxy('files.pythonhosted.org', '.pythonhosted.org'), true);
  assert.equal(probe.bypassesProxy('files.pythonhosted.org', 'pythonhosted.org'), true);
  assert.equal(probe.bypassesProxy('huggingface.co', '*.huggingface.co'), true);
  assert.equal(probe.bypassesProxy('pypi.org', ' PYPI.ORG:443 '), true);
  assert.equal(probe.bypassesProxy('anything.example', '*'), true);
  assert.equal(probe.bypassesProxy('notpypi.org', 'pypi.org'), false);
  assert.equal(probe.bypassesProxy('pypi.org', ''), false);
});

test('the system proxy is read from the first usable PAC-style entry', () => {
  assert.equal(probe.parseSystemRule('PROXY proxy.example.com:911; DIRECT'), 'http://proxy.example.com:911');
  assert.equal(probe.parseSystemRule('HTTPS secure.example.com:443'), 'https://secure.example.com:443');
  assert.equal(probe.parseSystemRule('DIRECT'), '');
  // pip cannot use SOCKS without an extra package.
  assert.equal(probe.parseSystemRule('SOCKS5 socks.example.com:1080; DIRECT'), '');
  assert.equal(probe.parseSystemRule('SOCKS5 socks.example.com:1080'), null);
  assert.equal(probe.parseSystemRule(''), null);
});

test('candidates come most specific first, without duplicates, ending with direct', () => {
  const saved = { httpProxy: 'http://saved:1', httpsProxy: 'http://saved:1', noProxy: 'localhost' };
  const list = probe.candidates({
    saved,
    env: { httpProxy: 'http://saved:1', httpsProxy: 'http://saved:1', noProxy: '' },
    system: 'http://system:2',
  });
  assert.deepEqual(list.map((entry) => entry.source), ['saved', 'system', 'direct']);

  // A saved file without a proxy is a choice of a direct connection.
  const direct = probe.candidates({ saved: { httpProxy: '', httpsProxy: '', noProxy: '' } });
  assert.deepEqual(direct.map((entry) => entry.source), ['saved']);

  assert.deepEqual(probe.candidates().map((entry) => entry.source), ['direct']);
});

// ---------------------------------------------------------------------------
// Evaluation and the changes the recommended path makes
// ---------------------------------------------------------------------------

test('evaluate() needs the packages and a model hub', () => {
  assert.equal(probe.evaluate(allOk).ok, true);
  assert.equal(probe.evaluate(withFailing(['packages'])).ok, false);
  assert.equal(probe.evaluate(withFailing(['huggingface', 'modelscope'])).ok, false);
});

test('evaluate() suggests the other hub when only the configured one fails', () => {
  const evaluation = probe.evaluate(withFailing(['huggingface']), { hub: 'huggingface' });
  assert.equal(evaluation.suggestedHub, 'modelscope');
  // Applying the suggestion is what makes the settings work.
  assert.equal(evaluation.ok, true);

  assert.equal(probe.evaluate(withFailing(['modelscope']), { hub: 'huggingface' }).suggestedHub, null);
});

test('evaluate() needs the installer hosts only while there is an installer to download', () => {
  const failing = withFailing(['installers']);
  assert.equal(probe.evaluate(failing, { needInstallers: false }).ok, true);
  assert.equal(probe.evaluate(failing, { needInstallers: true }).ok, false);

  assert.equal(probe.needsInstallers([{ stepId: 'python' }, { stepId: 'venv' }]), true);
  assert.equal(probe.needsInstallers([{ stepId: 'venv' }, { stepId: 'gradingVenv' }]), false);
});

test('a working proxy that is not saved yet is saved, with a loopback NO_PROXY', () => {
  const chosen = { source: 'system', httpProxy: 'http://proxy:911', httpsProxy: 'http://proxy:911', noProxy: '' };
  assert.deepEqual(probe.recommendedChanges({ saved: null, chosen }, probe.evaluate(allOk)), [
    { file: 'proxy', path: 'httpProxy', value: 'http://proxy:911' },
    { file: 'proxy', path: 'httpsProxy', value: 'http://proxy:911' },
    { file: 'proxy', path: 'noProxy', value: probe.DEFAULT_NO_PROXY },
  ]);
});

test('nothing is written when the saved settings work, or when direct works with nothing saved', () => {
  const saved = { httpProxy: 'http://proxy:911', httpsProxy: 'http://proxy:911', noProxy: 'localhost' };
  assert.deepEqual(probe.recommendedChanges({ saved, chosen: { ...saved, source: 'saved' } }, probe.evaluate(allOk)), []);

  const direct = { source: 'direct', httpProxy: '', httpsProxy: '', noProxy: '' };
  assert.deepEqual(probe.recommendedChanges({ saved: null, chosen: direct }, probe.evaluate(allOk)), []);
});

test('a saved proxy that failed where direct worked is cleared, keeping NO_PROXY', () => {
  const saved = { httpProxy: 'http://old:1', httpsProxy: 'http://old:1', noProxy: 'intranet' };
  const direct = { source: 'direct', httpProxy: '', httpsProxy: '', noProxy: '' };
  assert.deepEqual(probe.recommendedChanges({ saved, chosen: direct }, probe.evaluate(allOk)), [
    { file: 'proxy', path: 'httpProxy', value: '' },
    { file: 'proxy', path: 'httpsProxy', value: '' },
    { file: 'proxy', path: 'noProxy', value: 'intranet' },
  ]);
});

test('the suggested hub becomes a config.yaml change', () => {
  const evaluation = probe.evaluate(withFailing(['huggingface']), { hub: 'huggingface' });
  assert.deepEqual(probe.recommendedChanges({ saved: null, chosen: null }, evaluation), [
    { file: 'config', path: 'models.model_hub', value: 'modelscope' },
  ]);
});

// ---------------------------------------------------------------------------
// test(): trying the candidates
// ---------------------------------------------------------------------------

test('test() stops at the first candidate that reaches the package hosts', async () => {
  const tried = [];
  const outcome = await probe.test({
    saved: { httpProxy: 'http://stale:1', httpsProxy: 'http://stale:1', noProxy: '' },
    resolveSystem: async () => 'PROXY corporate:911; DIRECT',
    probe: async (candidate) => {
      tried.push(candidate.source);
      return candidate.source === 'system' ? allOk : withFailing(['packages']);
    },
  });
  assert.deepEqual(tried, ['saved', 'system']);
  assert.equal(outcome.chosen.source, 'system');
  assert.equal(outcome.chosen.httpsProxy, 'http://corporate:911');
  assert.equal(outcome.tried.length, 2);
});

test('test() reports every candidate when none works, and survives a failing system lookup', async () => {
  const outcome = await probe.test({
    resolveSystem: async () => {
      throw new Error('no session');
    },
    probe: async () => withFailing(['packages']),
  });
  assert.equal(outcome.chosen, null);
  assert.deepEqual(outcome.tried.map((entry) => entry.source), ['direct']);
});

test('typed values are validated before anything connects', async () => {
  let probed = false;
  await assert.rejects(
    probe.probeEntered({ httpProxy: 'proxy:911', httpsProxy: '', noProxy: '' }, {
      probe: async () => {
        probed = true;
        return allOk;
      },
    }),
    /HTTP_PROXY must be an http\(s\) URL/
  );
  assert.equal(probed, false);
});

test('typed values come back masked, with their evaluation', async () => {
  const outcome = await probe.probeEntered(
    { httpProxy: 'http://user:secret@proxy:911', httpsProxy: 'http://user:secret@proxy:911', noProxy: '' },
    { probe: async () => allOk }
  );
  assert.equal(outcome.candidate.httpsProxy, 'http://***@proxy:911');
  assert.equal(outcome.evaluation.ok, true);
});

// ---------------------------------------------------------------------------
// probeHost() against fake proxies
// ---------------------------------------------------------------------------

test('a proxy that wants credentials is reported as such', async (t) => {
  const port = await fakeProxy(t, (_request, socket) => {
    socket.end('HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="corp"\r\n\r\n');
  });
  const outcome = await probe.probeHost(TARGET, proxyAt(port), { timeoutMs: 5000 });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.cause, CAUSE.PROXY_AUTH);
});

test('a proxy that refuses the host is reported as refusing', async (t) => {
  const port = await fakeProxy(t, (_request, socket) => socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'));
  const outcome = await probe.probeHost(TARGET, proxyAt(port), { timeoutMs: 5000 });
  assert.equal(outcome.cause, CAUSE.PROXY_REFUSED);
  assert.equal(outcome.detail, '403');
});

test('a proxy nothing listens on is unreachable', async () => {
  const outcome = await probe.probeHost(TARGET, proxyAt(await closedPort()), { timeoutMs: 5000 });
  assert.equal(outcome.cause, CAUSE.PROXY_UNREACHABLE);
});

test('a proxy that accepts the tunnel request but never answers times out', async (t) => {
  const port = await fakeProxy(t, () => { });
  const outcome = await probe.probeHost(TARGET, proxyAt(port), { timeoutMs: 300 });
  assert.equal(outcome.cause, CAUSE.TIMEOUT);
});

test('credentials in the proxy URL are sent, decoded, as basic authentication', async (t) => {
  const expected = `Basic ${Buffer.from('user:p@ss word').toString('base64')}`;
  const port = await fakeProxy(t, (request, socket) => {
    if (request.headers['proxy-authorization'] !== expected) {
      socket.end('HTTP/1.1 407 Proxy Authentication Required\r\n\r\n');
      return;
    }
    // Open the tunnel, then drop it: the handshake fails, but not on the proxy.
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    socket.end();
  });

  const right = await probe.probeHost(TARGET, proxyAt(port, 'user:p%40ss%20word@'), { timeoutMs: 5000 });
  assert.equal(right.cause, CAUSE.CONNECTION);

  const wrong = await probe.probeHost(TARGET, proxyAt(port, 'user:nope@'), { timeoutMs: 5000 });
  assert.equal(wrong.cause, CAUSE.PROXY_AUTH);
});

test('a direct connection that is refused is a connection failure', async () => {
  const outcome = await probe.probeHost({ host: '127.0.0.1', port: await closedPort() }, null, { timeoutMs: 5000 });
  assert.equal(outcome.ok, false);
  assert.equal(outcome.cause, CAUSE.CONNECTION);
});

test('a SOCKS proxy is reported as unsupported rather than tried', async () => {
  const outcome = await probe.probeHost(TARGET, probe.parseProxy('socks5://127.0.0.1:1080'));
  assert.equal(outcome.cause, CAUSE.UNSUPPORTED_PROXY);
});

test('NO_PROXY hosts skip the proxy', async (t) => {
  let tunnels = 0;
  const port = await fakeProxy(t, (_request, socket) => {
    tunnels += 1;
    socket.end('HTTP/1.1 403 Forbidden\r\n\r\n');
  });
  const closed = await closedPort();
  const results = await probe.probeCandidate(
    { source: 'entered', httpProxy: '', httpsProxy: `http://127.0.0.1:${port}`, noProxy: '127.0.0.1' },
    [{ host: '127.0.0.1', port: closed, group: 'packages' }],
    { timeoutMs: 5000 }
  );
  assert.equal(tunnels, 0);
  assert.equal(results[0].cause, CAUSE.CONNECTION);
});

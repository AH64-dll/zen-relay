/**
 * Probe tests: readiness must be proven through the served interface, so the
 * failure modes that matter are timeouts, 5xx, and a body that claims success
 * without meaning it.
 */

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

import { httpProbe, tcpProbe, waitUntil } from '../lib/probe.mjs';

/** @type {http.Server} */
let server;
let port;
/** @type {Set<import('node:net').Socket>} */
const sockets = new Set();

before(async () => {
  server = http.createServer((request, response) => {
    switch (request.url) {
      case '/ok':
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ ok: true, inflight: 0 }));
        return;
      case '/not-ok':
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ ok: false }));
        return;
      case '/error':
        response.writeHead(503);
        response.end('unavailable');
        return;
      case '/unauthorized':
        response.writeHead(401);
        response.end('sign in');
        return;
      case '/hang':
        // Never answer: the probe must time out instead of waiting forever.
        return;
      default:
        response.writeHead(404);
        response.end();
    }
  });
  server.on('connection', (socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  port = /** @type {import('node:net').AddressInfo} */ (server.address()).port;
});

after(async () => {
  for (const socket of sockets) socket.destroy();
  await new Promise((resolve) => server.close(resolve));
});

const url = (path) => `http://127.0.0.1:${port}${path}`;

test('a 200 with {"ok":true} passes a JSON readiness probe', async () => {
  const result = await httpProbe(url('/ok'), { timeoutMs: 2_000, expectJsonOk: true });
  assert.equal(result.ok, true);
  assert.equal(result.status, 200);
  assert.equal(/** @type {any} */ (result.json).inflight, 0);
});

test('a 200 that is not actually ready fails a JSON readiness probe', async () => {
  const result = await httpProbe(url('/not-ok'), { timeoutMs: 2_000, expectJsonOk: true });
  assert.equal(result.ok, false);
  assert.match(result.detail, /not \{"ok":true\}/);
});

test('a 5xx fails; a 401 counts as alive for a bare liveness probe', async () => {
  const failed = await httpProbe(url('/error'), { timeoutMs: 2_000 });
  assert.equal(failed.ok, false);
  assert.equal(failed.status, 503);

  const alive = await httpProbe(url('/unauthorized'), { timeoutMs: 2_000 });
  assert.equal(alive.ok, true, 'a 401 proves the HTTP server answers');
});

test('a hung server times out instead of blocking', async () => {
  const started = Date.now();
  const result = await httpProbe(url('/hang'), { timeoutMs: 300 });
  const elapsed = Date.now() - started;
  assert.equal(result.ok, false);
  assert.match(result.detail, /timeout/);
  assert.ok(elapsed < 2_000, `probe returned after ${elapsed}ms`);
});

test('a refused connection is reported, not thrown', async () => {
  const result = await httpProbe('http://127.0.0.1:1/', { timeoutMs: 500 });
  assert.equal(result.ok, false);
  assert.match(result.detail, /refused|ECONNREFUSED/);
});

test('tcpProbe distinguishes an open port from a closed one', async () => {
  assert.equal((await tcpProbe('127.0.0.1', port, 1_000)).ok, true);
  const closed = await tcpProbe('127.0.0.1', port + 1 === 65_536 ? 1 : port + 1, 500);
  assert.equal(closed.ok, false);
});

test('waitUntil retries until success and reports the attempt count', async () => {
  let attempts = 0;
  const result = await waitUntil(
    async () => {
      attempts += 1;
      return { ok: attempts >= 3, detail: `attempt ${attempts}` };
    },
    { timeoutMs: 2_000, intervalMs: 10 },
  );
  assert.equal(result.ok, true);
  assert.equal(result.attempts, 3);
});

test('waitUntil gives up at the deadline', async () => {
  const started = Date.now();
  const result = await waitUntil(async () => ({ ok: false, detail: 'nope' }), { timeoutMs: 60, intervalMs: 10 });
  assert.equal(result.ok, false);
  assert.ok(Date.now() - started >= 50);
});

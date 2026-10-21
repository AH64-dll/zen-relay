/**
 * Readiness and liveness probes.
 *
 * Readiness is not "the process exists": every component is verified through the
 * interface it actually serves, with a hard timeout, so a wedged event loop or a
 * listener that accepts but never answers is caught the same way a crash is.
 *
 * @module dsh-stack/probe
 */

import http from 'node:http';
import https from 'node:https';
import net from 'node:net';

/**
 * @typedef {object} ProbeResult
 * @property {boolean} ok
 * @property {number} ms
 * @property {number} [status]
 * @property {string} detail
 * @property {unknown} [json]
 */

/**
 * HTTP(S) probe. `ok` means: the server answered within the timeout, with a
 * status below 500, and — when `expectJsonOk` is set — a JSON body whose `ok`
 * field is truthy.
 *
 * @param {string} url
 * @param {{timeoutMs?: number, expectJsonOk?: boolean, headers?: Record<string, string>}} [options]
 * @returns {Promise<ProbeResult>}
 */
export function httpProbe(url, options = {}) {
  const timeoutMs = options.timeoutMs ?? 5_000;
  const started = Date.now();
  let parsed;
  try {
    parsed = new URL(url);
  } catch (error) {
    return Promise.resolve({ ok: false, ms: 0, detail: `invalid url: ${String(error)}` });
  }
  const transport = parsed.protocol === 'https:' ? https : http;

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      resolve({ ...result, ms: Date.now() - started });
    };
    /** @type {http.ClientRequest} */
    let request;
    try {
      request = transport.request(
        parsed,
        {
          method: 'GET',
          timeout: timeoutMs,
          headers: {
            'user-agent': 'dsh-stack/1.0 (health probe)',
            accept: 'application/json, text/html;q=0.9, */*;q=0.8',
            ...(options.headers ?? {}),
          },
        },
        (response) => {
          const status = response.statusCode ?? 0;
          let body = '';
          response.setEncoding('utf8');
          response.on('data', (chunk) => {
            if (body.length < 64 * 1024) body += chunk;
          });
          response.on('end', () => {
            let json;
            if (body.length > 0 && (body.startsWith('{') || body.startsWith('['))) {
              try {
                json = JSON.parse(body);
              } catch {
                json = undefined;
              }
            }
            let ok = status > 0 && status < 500;
            let detail = `HTTP ${status}`;
            if (ok && options.expectJsonOk === true) {
              if (json === undefined || typeof json !== 'object' || !/** @type {any} */ (json).ok) {
                ok = false;
                detail = `HTTP ${status} but body is not {"ok":true}`;
              }
            }
            finish({ ok, status, detail, json });
          });
          response.on('error', (error) => finish({ ok: false, status, detail: `response error: ${error.message}` }));
        },
      );
    } catch (error) {
      finish({ ok: false, detail: `request failed: ${/** @type {Error} */ (error).message}` });
      return;
    }

    request.on('timeout', () => {
      request.destroy();
      finish({ ok: false, detail: `timeout after ${timeoutMs}ms` });
    });
    request.on('error', (error) => {
      const code = /** @type {any} */ (error)?.code;
      finish({ ok: false, detail: code === 'ECONNREFUSED' ? 'connection refused' : code ?? /** @type {Error} */ (error).message });
    });
    request.end();
  });
}

/**
 * TCP connect probe, used for the pre-boot network gate.
 * @param {string} host
 * @param {number} port
 * @param {number} timeoutMs
 * @returns {Promise<ProbeResult>}
 */
export function tcpProbe(host, port, timeoutMs = 5_000) {
  const started = Date.now();
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      resolve({ ...result, ms: Date.now() - started });
    };
    const socket = net.connect({ host, port, timeout: timeoutMs });
    socket.once('connect', () => {
      socket.destroy();
      finish({ ok: true, detail: `tcp ${host}:${port} reachable` });
    });
    socket.once('timeout', () => {
      socket.destroy();
      finish({ ok: false, detail: `tcp timeout after ${timeoutMs}ms` });
    });
    socket.once('error', (error) => {
      const code = /** @type {any} */ (error)?.code;
      finish({ ok: false, detail: code ?? /** @type {Error} */ (error).message });
    });
  });
}

/**
 * Poll `check` until it reports ok or the deadline passes.
 * @template T
 * @param {() => Promise<{ok: boolean, detail: string} & T>} check
 * @param {{timeoutMs: number, intervalMs?: number, signal?: AbortSignal, onAttempt?: (result: any) => void}} options
 * @returns {Promise<{ok: boolean, detail: string, attempts: number} & T>}
 */
export async function waitUntil(check, options) {
  const intervalMs = options.intervalMs ?? 500;
  const deadline = Date.now() + options.timeoutMs;
  let attempts = 0;
  let last = { ok: false, detail: 'not attempted' };
  for (;;) {
    attempts += 1;
    last = await check();
    options.onAttempt?.(last);
    if (last.ok) return { ...last, attempts };
    if (options.signal?.aborted === true) return { ...last, attempts, detail: `${last.detail} (aborted)` };
    if (Date.now() >= deadline) return { ...last, attempts };
    await sleep(Math.min(intervalMs, Math.max(0, deadline - Date.now())));
  }
}

/** @param {number} ms */
export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

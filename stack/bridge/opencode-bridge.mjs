#!/usr/bin/env node
/**
 * opencode-bridge — an OpenAI-compatible front end for a local OpenCode server.
 *
 * The DeepSeek Harness speaks the OpenAI chat-completions protocol. OpenCode
 * speaks its own session API. This process sits between the two:
 *
 *     Harness ──OpenAI/chat-completions──▶ opencode-bridge ──session API──▶
 *     OpenCode server ──▶ opencode.ai (as the official client)
 *
 * A request becomes one OpenCode session prompt; the assistant's text is
 * returned as a chat completion. Because the model call is made by OpenCode
 * itself, the upstream sees the official client and the provider's free tier
 * accepts it — no fingerprint is forged and no credential is copied.
 *
 * Trade-off, stated plainly: OpenCode runs its *own* agent loop with its own
 * tools inside the session. The Harness receives the answer, not the tool
 * calls; the tools that ran are OpenCode's, in OpenCode's session. The
 * Harness is a front end to OpenCode, not a second agent on top of it.
 *
 * Configuration (environment):
 *   PORT              listen port                       (default 8902)
 *   HOST              listen address                    (default 127.0.0.1)
 *   BRIDGE_API_KEY    comma-separated keys the /v1 API accepts; empty means
 *                     "loopback callers only"
 *   OPENCODE_BASE_URL local OpenCode server              (default http://127.0.0.1:49374)
 *   OPENCODE_BIN      `opencode` binary, used to start the server if needed
 *   OPENCODE_PASSWORD server password; discovered from the service config
 *   BRIDGE_TIMEOUT_MS per-request budget                 (default 300000)
 *   BRIDGE_DIRECTORY  working directory for the sessions (default: server's)
 *
 * @module dsh-stack/opencode-bridge
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { execFile, execFileSync } from 'node:child_process';

const VERSION = '1.0.0';

/** @typedef {ReturnType<typeof loadConfig>} BridgeConfig */

/**
 * Read the bridge configuration from the environment.
 * @param {NodeJS.ProcessEnv} env
 */
export function loadConfig(env = process.env) {
  const home = env.HOME ?? os.homedir();
  return {
    host: env.BRIDGE_HOST ?? '127.0.0.1',
    port: Number(env.PORT ?? 8902),
    /** Empty means "any loopback caller"; a non-empty list is required. */
    apiKeys: [
      ...(env.BRIDGE_API_KEY ?? '').split(',').map((k) => k.trim()),
      // The Harness sends the relay key; accept it so one secret covers both
      // routes and no second secret has to be stored.
      (env.OMP_ZEN_RELAY_KEY ?? '').trim(),
    ].filter((k) => k.length > 0),
    opencodeBaseUrl: (env.OPENCODE_BASE_URL ?? 'http://127.0.0.1:49374').replace(/\/+$/, ''),
    opencodeBin: env.OPENCODE_BIN ?? 'opencode',
    opencodePassword: env.OPENCODE_PASSWORD ?? null,
    serviceWrapper: (env.OPENCODE_SERVICE_WRAPPER ?? '').trim() || null,
    timeoutMs: Number(env.BRIDGE_TIMEOUT_MS ?? 300_000),
    directory: env.BRIDGE_DIRECTORY ?? null,
    version: VERSION,
    home,
  };
}

/**
 * Find the password of the local OpenCode service: the environment first, then
 * the service config file, then the CLI.
 * @param {BridgeConfig} config
 * @returns {string|null}
 */
export function discoverServicePassword(config) {
  if (config.opencodePassword) return config.opencodePassword;
  const file = path.join(config.home, '.config', 'opencode', 'service.json');
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (typeof parsed.password === 'string' && parsed.password.length > 0) return parsed.password;
  } catch {
    // no service config; fall through to the CLI
  }
  try {
    const out = execFileSync(config.opencodeBin, ['service', 'get', 'password'], {
      encoding: 'utf8',
      timeout: 15_000,
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    if (out.length > 0 && !out.startsWith('{')) return out;
  } catch {
    // the CLI is not available; the server may not need a password
  }
  return null;
}

/**
 * The conversation key: a client that keeps sending the same opening message
 * is treated as the same conversation, so the OpenCode session is reused and
 * the model keeps its memory.
 * @param {string} model
 * @param {Array<{role: string, content: unknown}>} messages
 */
export function conversationKey(model, messages) {
  const first = messages.find((m) => m.role === 'user');
  const system = messages.find((m) => m.role === 'system');
  const hash = crypto.createHash('sha256');
  hash.update(model);
  hash.update('\u0000');
  hash.update(typeof system?.content === 'string' ? system.content : '');
  hash.update('\u0000');
  hash.update(typeof first?.content === 'string' ? first.content : JSON.stringify(first?.content ?? ''));
  return hash.digest('hex').slice(0, 32);
}

/**
 * Flatten a chat message into plain text.
 * @param {{role: string, content: unknown}} message
 */
export function messageToText(message) {
  const { content } = message;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === 'string') return part;
        if (part?.type === 'text') return String(part.text ?? '');
        return '';
      })
      .filter((text) => text.length > 0)
      .join('\n');
  }
  if (content === undefined || content === null) return '';
  return JSON.stringify(content);
}

/**
 * The prompt text for the messages that have not been sent to OpenCode yet.
 * Only user turns are forwarded: the assistant turns were produced by
 * OpenCode itself and are already part of its session.
 * @param {Array<{role: string, content: unknown}>} messages
 * @param {number} alreadySent
 */
export function pendingUserText(messages, alreadySent) {
  const fresh = messages.slice(Math.max(0, alreadySent));
  const parts = [];
  for (const message of fresh) {
    if (message.role !== 'user') continue;
    const text = messageToText(message);
    if (text.length > 0) parts.push(text);
  }
  return parts.join('\n\n');
}

/** A tiny JSON error body in the OpenAI error shape. */
export function errorBody(message, type = 'bridge_error', code = null) {
  return { error: { message, type, ...(code === null ? {} : { code }) } };
}

/** Client for the local OpenCode server. */
export class OpenCodeClient {
  /** @param {BridgeConfig} config */
  constructor(config) {
    this.config = config;
    /** @type {string|null} */
    this.password = null;
    /** @type {Map<string, {resolve: Function, reject: Function, text: string, done: boolean}>} */
    this.waiters = new Map();
    /** @type {Map<string, string>} */
    this.texts = new Map();
    this.events = null;
    this.reconnecting = false;
  }

  /** @returns {string} */
  authorization() {
    if (this.password === null) this.password = discoverServicePassword(this.config);
    if (this.password === null) return '';
    return `Basic ${Buffer.from(`opencode:${this.password}`).toString('base64')}`;
  }

  /**
   * One JSON request against the OpenCode server.
   * @param {string} route
   * @param {{method?: string, body?: unknown}} [options]
   */
  async request(route, options = {}) {
    const headers = { accept: 'application/json' };
    const authorization = this.authorization();
    if (authorization.length > 0) headers.authorization = authorization;
    if (options.body !== undefined) headers['content-type'] = 'application/json';
    const response = await fetch(`${this.config.opencodeBaseUrl}${route}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: AbortSignal.timeout(this.config.timeoutMs),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text.length > 0 ? JSON.parse(text) : null;
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload?.message ?? payload?._tag ?? text.slice(0, 200);
      const error = new Error(`opencode ${response.status}: ${detail}`);
      // A stale password is the one failure worth retrying with fresh state.
      if (response.status === 401) {
        this.password = null;
        error.code = 'unauthorized';
      }
      throw error;
    }
    return payload;
  }

  /** Ask the CLI to bring the background server up, then wait for it. */
  async ensureServer() {
    const probe = async () => {
      try {
        await this.request('/api/provider');
        return true;
      } catch {
        return false;
      }
    };
    if (await probe()) return true;
    // Launch the service through the proxychains wrapper when configured, so
    // its upstream connections get rotating egress while keeping the real
    // OpenCode client identity (required by the free-tier gate).
    const launcher = this.config.serviceWrapper;
    try {
      await new Promise((resolve) => {
        if (launcher !== null) {
          execFile(launcher, [], { timeout: 60_000, detached: true }, () => resolve(undefined));
        } else {
          execFile(this.config.opencodeBin, ['service', 'start'], { timeout: 60_000 }, () => resolve(undefined));
        }
      });
    } catch {
      // the launcher may fail; the server may already be up
    }
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      if (await probe()) return true;
      await new Promise((resolve) => setTimeout(resolve, 1_000));
    }
    return false;
  }

  /** @returns {Promise<Array<{id: string, name: string, providerID: string}>>} */
  async listModels() {
    const payload = await this.request('/api/model');
    const models = Array.isArray(payload?.data) ? payload.data : [];
    return models.map((m) => ({ id: String(m.id), name: String(m.name ?? m.id), providerID: String(m.providerID ?? '') }));
  }

  /** @param {string} title */
  async createSession(title) {
    const query = this.config.directory === null ? '' : `?directory=${encodeURIComponent(this.config.directory)}`;
    const payload = await this.request(`/api/session${query}`, {
      method: 'POST',
      body: { title },
    });
    const id = payload?.data?.id ?? payload?.id;
    if (typeof id !== 'string') throw new Error('opencode did not return a session id');
    return id;
  }

  /**
   * Pin the model of a session.
   * @param {string} sessionID
   * @param {string} model
   */
  async setModel(sessionID, model) {
    await this.request(`/api/session/${sessionID}/model`, {
      method: 'POST',
      body: { model: { id: model, providerID: 'opencode' } },
    });
  }

  /**
   * Send a prompt; the answer arrives through the event stream.
   * @param {string} sessionID
   * @param {string} text
   */
  async prompt(sessionID, text) {
    await this.request(`/api/session/${sessionID}/prompt`, {
      method: 'POST',
      body: { text },
    });
  }

  /**
   * Resolve when the session's execution ends; collects the assistant text.
   * @param {string} sessionID
   * @param {number} timeoutMs
   */
  waitForExecution(sessionID, timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(sessionID);
        reject(new Error('timed out waiting for the OpenCode session'));
      }, timeoutMs);
      this.waiters.set(sessionID, {
        text: '',
        resolve: (value) => {
          clearTimeout(timer);
          this.waiters.delete(sessionID);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          this.waiters.delete(sessionID);
          reject(error);
        },
      });
      this.texts.set(sessionID, '');
    });
  }

  /** Feed one event into the waiters. */
  handleEvent(event) {
    const type = event?.type;
    const data = event?.data ?? {};
    const sessionID = data.sessionID;
    if (typeof sessionID !== 'string') return;
    if (type === 'session.text.delta') {
      const current = this.texts.get(sessionID) ?? '';
      this.texts.set(sessionID, current + String(data.delta ?? ''));
      const waiter = this.waiters.get(sessionID);
      if (waiter) waiter.text = this.texts.get(sessionID) ?? '';
      return;
    }
    if (type === 'session.text.ended') {
      const waiter = this.waiters.get(sessionID);
      if (waiter && typeof data.text === 'string' && data.text.length > 0) waiter.text = data.text;
      return;
    }
    if (type === 'session.execution.succeeded') {
      const waiter = this.waiters.get(sessionID);
      if (waiter) waiter.resolve(waiter.text);
      return;
    }
    if (type === 'session.error' || type === 'session.execution.failed') {
      const waiter = this.waiters.get(sessionID);
      if (waiter) waiter.reject(new Error(String(data.error?.message ?? data.message ?? 'OpenCode session failed')));
    }
  }

  /** Open (and keep open) the server-sent event stream. */
  connectEvents() {
    if (this.events !== null) return;
    const headers = { accept: 'text/event-stream' };
    const authorization = this.authorization();
    if (authorization.length > 0) headers.authorization = authorization;
    const controller = new AbortController();
    const request = fetch(`${this.config.opencodeBaseUrl}/api/event`, {
      headers,
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok || response.body === null) throw new Error(`event stream: HTTP ${response.status}`);
      const decoder = new TextDecoder();
      let buffer = '';
      for await (const chunk of response.body) {
        buffer += decoder.decode(chunk, { stream: true });
        let index = buffer.indexOf('\n\n');
        while (index !== -1) {
          const frame = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data:')) continue;
            const payload = line.slice(5).trim();
            if (payload.length === 0) continue;
            try {
              this.handleEvent(JSON.parse(payload));
            } catch {
              // a malformed frame must not kill the stream
            }
          }
          index = buffer.indexOf('\n\n');
        }
      }
    }).catch((error) => {
      if (controller.signal.aborted) return;
      this.logger?.('event-stream-error', error?.message ?? String(error));
    }).finally(() => {
      this.events = null;
      if (!this.reconnecting) {
        this.reconnecting = true;
        setTimeout(() => {
          this.reconnecting = false;
          this.connectEvents();
        }, 1_000).unref?.();
      }
    });
    this.events = request;
  }
}

/** The bridge itself. */
export class Bridge {
  /** @param {BridgeConfig} config */
  constructor(config) {
    this.config = config;
    this.client = new OpenCodeClient(config);
    /** @type {Map<string, {sessionID: string, sent: number}>} */
    this.conversations = new Map();
    /** @type {Map<string, Promise<unknown>>} */
    this.locks = new Map();
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  authorize(req, res) {
    if (this.config.apiKeys.length === 0) {
      // No key configured: only loopback callers are allowed.
      const remote = req.socket.remoteAddress ?? '';
      if (remote === '127.0.0.1' || remote === '::1' || remote === '::ffff:127.0.0.1') return true;
      this.send(res, 401, errorBody('bridge requires a key on non-loopback callers', 'authentication_error'));
      return false;
    }
    const header = req.headers.authorization ?? '';
    const token = header.startsWith('Bearer ') ? header.slice(7).trim() : '';
    if (this.config.apiKeys.includes(token)) return true;
    this.send(res, 401, errorBody('invalid bridge key', 'authentication_error'));
    return false;
  }

  /**
   * @param {import('node:http').ServerResponse} res
   * @param {number} status
   * @param {unknown} body
   */
  send(res, status, body) {
    const payload = JSON.stringify(body);
    res.writeHead(status, {
      'content-type': 'application/json',
      'content-length': Buffer.byteLength(payload),
    });
    res.end(payload);
  }

  /**
   * Run one chat completion: reuse or create an OpenCode session, send the
   * new user turns, wait for the answer.
   * @param {string} model
   * @param {Array<{role: string, content: unknown}>} messages
   * @param {(text: string) => void} [onDelta]
   */
  async complete(model, messages, onDelta) {
    await this.client.ensureServer();
    this.client.connectEvents();

    const key = conversationKey(model, messages);
    let state = this.conversations.get(key);
    if (state !== undefined && state.sent > messages.length) {
      // The client rewound its history: start a fresh conversation.
      state = undefined;
    }
    if (state === undefined) {
      const sessionID = await this.client.createSession(`harness:${model}`);
      await this.client.setModel(sessionID, model);
      state = { sessionID, sent: 0 };
      this.conversations.set(key, state);
    }

    const text = pendingUserText(messages, state.sent);
    state.sent = messages.length;
    if (text.length === 0) {
      // Nothing new to say: return what the session already produced.
      return { text: this.client.texts.get(state.sessionID) ?? '', sessionID: state.sessionID, usage: null };
    }

    const waiter = this.client.waitForExecution(state.sessionID, this.config.timeoutMs);
    await this.client.prompt(state.sessionID, text);
    const answer = await waiter;
    if (onDelta !== undefined && answer.length > 0) onDelta(answer);
    return { text: answer, sessionID: state.sessionID, usage: null };
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  async handleChatCompletions(req, res) {
    const body = await readJson(req);
    if (body === null) {
      this.send(res, 400, errorBody('invalid JSON body', 'invalid_request_error'));
      return;
    }
    const model = typeof body.model === 'string' ? body.model : 'deepseek-v4.1-flash-free';
    const messages = Array.isArray(body.messages) ? body.messages : [];
    if (messages.length === 0) {
      this.send(res, 400, errorBody('messages must not be empty', 'invalid_request_error'));
      return;
    }

    const key = conversationKey(model, messages);
    const previous = this.locks.get(key) ?? Promise.resolve();
    const run = previous.then(() => this.runCompletion(body, model, messages, res));
    this.locks.set(key, run.catch(() => undefined));
    await run;
  }

  /**
   * @param {any} body
   * @param {string} model
   * @param {Array<{role: string, content: unknown}>} messages
   * @param {import('node:http').ServerResponse} res
   */
  async runCompletion(body, model, messages, res) {
    try {
      if (body.stream === true) {
        await this.streamCompletion(res, model, messages);
        return;
      }
      const { text } = await this.complete(model, messages);
      this.send(res, 200, {
        id: `chatcmpl-${crypto.randomUUID()}`,
        object: 'chat.completion',
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [
          {
            index: 0,
            message: { role: 'assistant', content: text },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      });
    } catch (error) {
      this.send(res, 502, errorBody(error?.message ?? String(error), 'bridge_error'));
    }
  }

  /**
   * @param {import('node:http').ServerResponse} res
   * @param {string} model
   * @param {Array<{role: string, content: unknown}>} messages
   */
  async streamCompletion(res, model, messages) {
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    });
    const write = (payload) => res.write(`data: ${JSON.stringify(payload)}\n\n`);
    const id = `chatcmpl-${crypto.randomUUID()}`;
    const created = Math.floor(Date.now() / 1000);
    write({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }] });
    try {
      const { text } = await this.complete(model, messages);
      write({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { content: text }, finish_reason: null }] });
      write({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
    } catch (error) {
      write({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: {}, finish_reason: 'error' }], error: { message: error?.message ?? String(error) } });
    }
    res.write('data: [DONE]\n\n');
    res.end();
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  async handleModels(req, res) {
    try {
      await this.client.ensureServer();
      const models = await this.client.listModels();
      this.send(res, 200, {
        object: 'list',
        data: models.map((m) => ({
          id: m.id,
          object: 'model',
          created: Math.floor(Date.now() / 1000),
          owned_by: m.providerID === '' ? 'opencode' : m.providerID,
        })),
      });
    } catch (error) {
      this.send(res, 502, errorBody(error?.message ?? String(error), 'bridge_error'));
    }
  }

  /**
   * @param {import('node:http').IncomingMessage} req
   * @param {import('node:http').ServerResponse} res
   */
  async handleHealth(req, res) {
    let reachable = false;
    let models = 0;
    try {
      await this.client.ensureServer();
      models = (await this.client.listModels()).length;
      reachable = true;
    } catch {
      reachable = false;
    }
    this.send(res, 200, {
      ok: reachable,
      version: VERSION,
      opencode: { url: this.config.opencodeBaseUrl, reachable },
      models,
    });
  }

  /** @param {import('node:http').IncomingMessage} req @param {import('node:http').ServerResponse} res */
  async route(req, res) {
    const url = new URL(req.url ?? '/', 'http://bridge.local');
    const route = url.pathname.replace(/\/+$/, '') || '/';
    if (route === '/healthz' || route === '/health') {
      await this.handleHealth(req, res);
      return;
    }
    if (!this.authorize(req, res)) return;
    if (route === '/v1/models' || route === '/models') {
      await this.handleModels(req, res);
      return;
    }
    if (route === '/v1/chat/completions' || route === '/chat/completions') {
      if (req.method !== 'POST') {
        this.send(res, 405, errorBody('method not allowed', 'invalid_request_error'));
        return;
      }
      await this.handleChatCompletions(req, res);
      return;
    }
    this.send(res, 404, errorBody(`no route for ${route}`, 'not_found'));
  }
}

/**
 * Read and parse a JSON request body.
 * @param {import('node:http').IncomingMessage} req
 * @returns {Promise<any|null>}
 */
export function readJson(req) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > 32 * 1024 * 1024) {
        req.destroy();
        resolve(null);
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch {
        resolve(null);
      }
    });
    req.on('error', () => resolve(null));
  });
}

/**
 * Start the bridge.
 * @param {BridgeConfig} [config]
 */
export async function main(config = loadConfig()) {
  const bridge = new Bridge(config);
  const server = http.createServer((req, res) => {
    bridge.route(req, res).catch((error) => {
      try {
        bridge.send(res, 500, errorBody(error?.message ?? String(error), 'bridge_error'));
      } catch {
        res.end();
      }
    });
  });
  server.on('clientError', (_error, socket) => socket.end('HTTP/1.1 400 Bad Request\r\n\r\n'));
  await new Promise((resolve) => server.listen(config.port, config.host, resolve));
  process.stdout.write(`opencode-bridge ${VERSION} listening on http://${config.host}:${config.port} (opencode ${config.opencodeBaseUrl})\n`);
  const shutdown = () => {
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3_000).unref();
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
  return server;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    process.stderr.write(`opencode-bridge failed: ${error?.message ?? String(error)}\n`);
    process.exit(1);
  });
}

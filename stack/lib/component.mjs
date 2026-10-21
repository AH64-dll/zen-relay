/**
 * One supervised child process.
 *
 * A component owns exactly one process group: the proxy, the tunnel and the
 * Harness each get their own group so a restart can never orphan the MCP
 * servers or browser helpers a previous generation spawned. Readiness is
 * decided by a caller-supplied probe, never by "the pid exists".
 *
 * @module dsh-stack/component
 */

import { spawn } from 'node:child_process';

import { sleep, waitUntil } from './probe.mjs';

/** Maximum bytes buffered for one unterminated line of child output. */
const MAX_LINE_BYTES = 1024 * 1024;

/**
 * @typedef {object} ComponentOptions
 * @property {string} name
 * @property {string[]} command argv, `command[0]` is the executable
 * @property {string} [cwd]
 * @property {NodeJS.ProcessEnv} [env]
 * @property {{info: Function, warn: Function, debug: Function}} logger
 * @property {(line: string, stream: 'stdout'|'stderr') => void} [onLine]
 * @property {(info: {code: number|null, signal: string|null}) => void} [onExit]
 * @property {() => Promise<{ok: boolean, detail: string}>} [probe]
 * @property {() => Promise<{ok: boolean, detail: string}>} [ready]
 * @property {number} [readyTimeoutMs]
 */

export class Component {
  /** @param {ComponentOptions} options */
  constructor(options) {
    this.name = options.name;
    this.command = options.command;
    this.cwd = options.cwd;
    this.env = options.env;
    this.logger = options.logger;
    this.onLine = options.onLine;
    this.onExitHook = options.onExit;
    this.probeFn = options.probe;
    this.readyFn = options.ready;
    this.readyTimeoutMs = options.readyTimeoutMs ?? 120_000;

    /** @type {'stopped'|'starting'|'ready'|'stopping'} */
    this.state = 'stopped';
    /** @type {import('node:child_process').ChildProcessWithoutNullStreams|undefined} */
    this.child = undefined;
    this.pid = undefined;
    this.startedAt = undefined;
    this.readyAt = undefined;
    this.exit = undefined;
    /** @type {string|undefined} */
    this.lastError = undefined;
    /** @type {string|undefined} */
    this.lastProbeDetail = undefined;
    this.stopping = false;
    /** @type {(() => void)|undefined} */
    this.resolveExit = undefined;
    this.consecutiveFailures = 0;
  }

  /** Whether the child process object exists and has not exited. */
  get alive() {
    return this.child !== undefined && this.child.exitCode === null && this.child.signalCode === null;
  }

  /** Whether readiness has been observed and the process has not exited. */
  get readyNow() {
    return this.state === 'ready' && this.alive;
  }

  /**
   * Spawn the process and wait until `ready()` reports success.
   * @throws when the process cannot spawn, exits during startup, or never becomes ready.
   */
  async start() {
    if (this.alive) return;
    const [executable, ...args] = this.command;
    if (typeof executable !== 'string' || executable.length === 0) throw new Error(`${this.name}: empty command`);
    this.state = 'starting';
    this.exit = undefined;
    this.lastError = undefined;
    this.startedAt = Date.now();
    this.readyAt = undefined;
    this.consecutiveFailures = 0;

    this.logger.info('spawn', { argv: this.command.join(' '), cwd: this.cwd });
    const child = spawn(executable, args, {
      cwd: this.cwd,
      env: this.env ?? process.env,
      // Own session/group: a restart kills the whole tree, never a stray MCP server.
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.child = child;
    this.pid = child.pid;

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    this.#pipe(child.stdout, 'stdout');
    this.#pipe(child.stderr, 'stderr');

    child.once('error', (error) => {
      this.lastError = `spawn failed: ${error.message}`;
      this.logger.warn('spawn-failed', { component: this.name, error: error.message });
      this.#finishExit(null, null);
    });
    child.once('exit', (code, signal) => {
      this.#finishExit(code, signal);
    });

    const ready = this.readyFn === undefined ? async () => ({ ok: true, detail: 'no ready probe' }) : this.readyFn;
    const result = await waitUntil(ready, {
      timeoutMs: this.readyTimeoutMs,
      intervalMs: 500,
    });
    if (!result.ok) {
      this.lastError = result.detail;
      throw new Error(`${this.name} not ready after ${this.readyTimeoutMs}ms: ${result.detail}`);
    }
    if (!this.alive) {
      this.lastError = this.lastError ?? 'exited during startup';
      throw new Error(`${this.name} exited during startup: ${this.lastError}`);
    }
    this.state = 'ready';
    this.readyAt = Date.now();
    this.lastProbeDetail = result.detail;
    this.logger.info('ready', { component: this.name, pid: this.pid, detail: result.detail, ms: this.readyAt - this.startedAt });
  }

  /**
   * Terminate the process group: SIGTERM, then SIGKILL after the grace period.
   * @param {{reason?: string, graceMs?: number}} [options]
   * @returns {Promise<void>}
   */
  async stop(options = {}) {
    if (!this.alive) {
      this.state = 'stopped';
      this.child = undefined;
      this.pid = undefined;
      return;
    }
    const graceMs = options.graceMs ?? 15_000;
    const child = this.child;
    this.stopping = true;
    this.state = 'stopping';
    this.logger.info('stop', { component: this.name, pid: child?.pid, reason: options.reason ?? 'requested', graceMs });

    const exited = new Promise((resolve) => {
      this.resolveExit = () => resolve(undefined);
      if (!this.alive) resolve(undefined);
    });
    this.#signal('SIGTERM');
    const timedOut = await Promise.race([exited.then(() => false), sleep(graceMs).then(() => true)]);
    if (timedOut) {
      this.logger.warn('stop-timeout', { component: this.name, pid: child?.pid, action: 'SIGKILL' });
      this.#signal('SIGKILL');
      await Promise.race([exited, sleep(5_000)]);
    }
    this.stopping = false;
    this.state = 'stopped';
    this.child = undefined;
    this.pid = undefined;
  }

  /**
   * One liveness probe. Does not change component state.
   * @returns {Promise<{ok: boolean, detail: string, ms?: number}>}
   */
  async check() {
    if (this.probeFn === undefined) {
      const ok = this.alive;
      return { ok, detail: ok ? 'process alive' : 'process not running' };
    }
    if (!this.alive) return { ok: false, detail: 'process not running' };
    const result = await this.probeFn();
    this.lastProbeDetail = result.detail;
    return result;
  }

  /** Send a signal to the whole process group (falling back to the process). */
  #signal(signal) {
    const pid = this.child?.pid;
    if (typeof pid !== 'number') return;
    try {
      process.kill(-pid, signal);
      return;
    } catch (error) {
      const code = /** @type {any} */ (error)?.code;
      if (code !== 'ESRCH' && code !== 'EPERM') {
        this.logger.debug('group-signal-failed', { component: this.name, pid, signal, code });
      }
    }
    try {
      this.child?.kill(signal);
    } catch {
      /* already gone */
    }
  }

  /** @param {import('node:stream').Readable} stream @param {'stdout'|'stderr'} which */
  #pipe(stream, which) {
    let buffer = '';
    stream.on('data', (chunk) => {
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index).replace(/\r$/, '');
        buffer = buffer.slice(index + 1);
        if (line.length > 0) this.#emitLine(line, which);
      }
      if (buffer.length > MAX_LINE_BYTES) {
        this.#emitLine(buffer.slice(0, MAX_LINE_BYTES), which);
        buffer = '';
      }
    });
    stream.on('error', () => {
      /* the exit handler owns the failure */
    });
  }

  /** @param {string} line @param {'stdout'|'stderr'} which */
  #emitLine(line, which) {
    // Child output is diagnostic volume: keep it in the log file, not on stderr.
    this.logger.debug('output', { component: this.name, stream: which, line: truncate(line, 2000) });
    try {
      this.onLine?.(line, which);
    } catch (error) {
      this.logger.debug('line-hook-failed', { component: this.name, error: /** @type {Error} */ (error).message });
    }
  }

  /** @param {number|null} code @param {string|null} signal */
  #finishExit(code, signal) {
    const wasRunning = this.state !== 'stopped';
    this.exit = { code, signal, at: Date.now(), uptimeMs: this.startedAt === undefined ? 0 : Date.now() - this.startedAt };
    this.state = 'stopped';
    this.child = undefined;
    this.pid = undefined;
    if (this.exit.code !== 0 || this.exit.signal !== null) {
      this.lastError = `exited code=${String(code)} signal=${String(signal)}`;
    }
    this.resolveExit?.();
    this.resolveExit = undefined;
    if (!this.stopping) {
      this.logger.warn('exited', { component: this.name, code, signal, uptimeMs: this.exit.uptimeMs });
      try {
        this.onExitHook?.({ code, signal });
      } catch {
        /* the supervisor loop owns recovery */
      }
    } else if (wasRunning) {
      this.logger.info('stopped', { component: this.name, code, signal });
    }
  }

  /** A serialisable view for the status document. */
  snapshot() {
    return {
      name: this.name,
      state: this.state,
      pid: this.pid ?? null,
      alive: this.alive,
      startedAt: this.startedAt ?? null,
      readyAt: this.readyAt ?? null,
      lastProbe: this.lastProbeDetail ?? null,
      lastError: this.lastError ?? null,
      consecutiveFailures: this.consecutiveFailures,
      exit: this.exit ?? null,
    };
  }
}

/** @param {string} value @param {number} max */
function truncate(value, max) {
  return value.length <= max ? value : `${value.slice(0, max)}…(${value.length} bytes)`;
}

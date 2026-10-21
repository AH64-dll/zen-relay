/**
 * The supervisor: strict boot ordering, health gating, self-healing restarts,
 * and the status document everything else reads.
 *
 * Boot order is enforced by *verification*, not by unit ordering alone:
 *
 *   network gate → proxy (ready) → tunnel (optional) → harness (ready) → READY=1
 *
 * The Harness is never started while the model proxy is unproven, because a
 * Harness booted against a dead route turns every session it opens into an
 * error turn. When the proxy is restarted later, the Harness is stopped first
 * and restarted after the proxy is ready again — the dependency is enforced in
 * both directions.
 *
 * @module dsh-stack/stack
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { Component } from './component.mjs';
import { createNotifier } from './sdnotify.mjs';
import { httpProbe, sleep, tcpProbe, waitUntil } from './probe.mjs';
import { fsyncDirSync, writeJsonAtomic, writeJsonAtomicSync, writeFileAtomic } from './atomic.mjs';

/** Delay before the first reconcile after a state change. */
const KICK_DELAY_MS = 1_500;

/**
 * @typedef {object} StackOptions
 * @property {any} config
 * @property {ReturnType<import('./log.mjs').createLogger>} logger
 * @property {ReturnType<typeof createNotifier>} [notify]
 */

export class Stack {
  /** @param {StackOptions} options */
  constructor(options) {
    this.config = options.config;
    this.logger = options.logger;
    this.notify = options.notify ?? createNotifier({ logger: options.logger });
    this.startedAt = Date.now();
    this.bootId = readBootId();
    this.stopping = false;
    this.reconciling = false;
    this.readyAnnounced = false;
    this.lastProgressAt = Date.now();
    this.fatalReason = undefined;

    /** @type {Record<string, {lastRestartAt: number, consecutive: number, history: number[], healthySince: number|undefined}>} */
    this.restartState = { proxy: freshRestart(), tunnel: freshRestart(), harness: freshRestart() };

    /** @type {{token?: string, host?: string, url?: string, published?: number}} */
    this.signIn = {};
    /** @type {{host?: string, url?: string, seenAt?: number}} */
    this.tunnel = {};
    /** @type {string|undefined} the `--trusted-host` the running harness was started with */
    this.harnessTrustedHost = undefined;

    /** @type {Record<string, Component|undefined>} */
    this.components = {};

    this.status = {
      version: 1,
      name: this.config.name,
      pid: process.pid,
      bootId: this.bootId,
      hostname: os.hostname(),
      startedAt: this.startedAt,
      updatedAt: this.startedAt,
      phase: 'starting',
      ready: false,
      components: {},
      signIn: {},
      tunnel: {},
      keeper: null,
      sessions: null,
      fatal: null,
    };

    /** @type {NodeJS.Timeout|undefined} */ this.healthTimer = undefined;
    /** @type {NodeJS.Timeout|undefined} */ this.watchdogTimer = undefined;
    /** @type {NodeJS.Timeout|undefined} */ this.kickTimer = undefined;
    /** @type {NodeJS.Timeout|undefined} */ this.verifyTimer = undefined;
  }

  // ---------------------------------------------------------------- lifecycle

  /** Boot the whole stack and keep it alive until shutdown. */
  async run() {
    this.logger.info('stack-start', {
      version: '1.0.0',
      config: this.config.configFile,
      stateDir: this.config.stateDir,
      node: process.version,
      pid: process.pid,
      notifySocket: this.notify.enabled ? String(this.notify.socket) : '(none)',
    });
    this.notify.status(`starting: proxy → tunnel → harness`);
    this.acquireInstanceLock();
    this.installSignalHandlers();
    this.startTimers();

    try {
      await this.waitForNetwork();
      await this.reconcile({ why: 'boot' });
      // Reconcile may still be retrying; announce readiness as soon as the
      // first full pass proves both required components, from the timer.
    } catch (error) {
      this.logger.error('boot-failed', { error: renderError(error) });
      this.notify.status(`boot failed: ${renderError(error)}`);
      // Keep the timers running: a transient failure must heal, not crash-loop.
    }
    this.logger.info('stack-running', { phase: this.status.phase });
    this.updateStatus();
  }

  /** Start the periodic work. */
  startTimers() {
    this.healthTimer = setInterval(() => {
      void this.reconcile({ why: 'health' });
    }, this.config.health.intervalMs);

    // The watchdog is deliberately its own timer: as long as the event loop is
    // alive and progress is being made, systemd must not kill us — even during
    // a slow (up to 5 minute) Harness boot.
    this.watchdogTimer = setInterval(() => {
      if (this.stopping || !this.config.health.watchdog) return;
      const stalledFor = Date.now() - this.lastProgressAt;
      if (stalledFor < this.config.health.escalateAfterMs) {
        this.notify.watchdog();
      } else {
        this.logger.warn('watchdog-withheld', { stalledMs: stalledFor });
      }
      this.updateStatus();
    }, Math.max(5_000, Math.floor(this.config.health.intervalMs / 2)));
  }

  /** Ask for one reconcile soon, coalescing bursts of triggers. */
  kick(why = 'kick') {
    if (this.stopping || this.kickTimer !== undefined) return;
    this.kickTimer = setTimeout(() => {
      this.kickTimer = undefined;
      void this.reconcile({ why });
    }, KICK_DELAY_MS);
  }

  installSignalHandlers() {
    const stop = (signal) => {
      this.logger.info('signal', { signal });
      void this.shutdown(`signal:${signal}`, 0);
    };
    process.on('SIGTERM', () => stop('SIGTERM'));
    process.on('SIGINT', () => stop('SIGINT'));
    process.on('SIGHUP', () => {
      this.logger.info('signal', { signal: 'SIGHUP', action: 'restart-harness' });
      void this.restartNow('harness', 'SIGHUP');
    });
    process.on('SIGUSR1', () => {
      this.updateStatus();
      this.logger.info('status-dump', { status: this.status });
    });
    process.on('SIGUSR2', () => {
      this.logger.info('signal', { signal: 'SIGUSR2', action: 'restart-proxy' });
      void this.restartNow('proxy', 'SIGUSR2');
    });
    process.on('uncaughtException', (error) => {
      this.logger.error('uncaught-exception', { error: renderError(error), stack: error.stack?.split('\n').slice(0, 6) });
      void this.shutdown('uncaught-exception', 1);
    });
    process.on('unhandledRejection', (reason) => {
      this.logger.error('unhandled-rejection', { error: renderError(reason) });
    });
  }

  /**
   * Graceful shutdown: Harness first (so in-flight requests can finish while the
   * proxy still answers), then the tunnel, then the proxy.
   * @param {string} reason
   * @param {number} code
   */
  async shutdown(reason, code) {
    if (this.stopping) return;
    this.stopping = true;
    this.logger.info('stack-stop', { reason, code });
    this.notify.stopping(`stopping: ${reason}`);
    clearInterval(this.healthTimer);
    clearInterval(this.watchdogTimer);
    clearTimeout(this.kickTimer);
    clearTimeout(this.verifyTimer);
    this.status.phase = 'stopping';
    this.updateStatus();

    const graceMs = this.config.restart.graceMs;
    await this.stopComponent('harness', reason, graceMs);
    await this.stopComponent('tunnel', reason, graceMs);
    await this.stopComponent('proxy', reason, graceMs);
    this.releaseInstanceLock();
    this.logger.info('stack-stopped', { reason });
    process.exit(code);
  }

  // --------------------------------------------------------------- reconcile

  /**
   * One convergence pass: start what should run, prove it, restart what wedged.
   * Reentrancy-safe; concurrent triggers coalesce.
   * @param {{why: string}} options
   */
  async reconcile(options) {
    if (this.stopping) return;
    if (this.reconciling) {
      this.kick('coalesced');
      return;
    }
    this.reconciling = true;
    this.lastProgressAt = Date.now();
    try {
      const config = this.config;
      this.buildComponents();

      // 1. Proxy — the root dependency.
      let proxyReady = false;
      if (config.proxy.mode === 'external') {
        proxyReady = await this.probeExternalProxy();
      } else {
        proxyReady = await this.converge('proxy');
      }
      if (!proxyReady) {
        this.logger.warn('proxy-not-ready', { why: options.why });
        await this.stopComponent('harness', 'proxy unavailable');
        this.status.phase = 'starting';
        this.updateStatus();
        this.kick('waiting-proxy');
        return;
      }

      // 2. Tunnel — optional; a failure degrades the public URL, never the boot.
      if (config.tunnel.mode === 'own') {
        const tunnelReady = await this.converge('tunnel');
        if (!tunnelReady) this.logger.warn('tunnel-not-ready', { why: options.why });
      } else if (config.tunnel.mode === 'off') {
        this.components.tunnel = undefined;
      }

      // 3. Harness — only behind a proven route.
      const host = this.tunnelTargetHost();
      if (host !== undefined && host !== this.harnessTrustedHost) {
        if (this.components.harness?.alive === true) {
          this.logger.info('harness-retrust', { from: this.harnessTrustedHost, to: host });
          await this.stopComponent('harness', 'trusted-host changed');
        }
        this.harnessTrustedHost = host;
      }
      const harnessReady = await this.converge('harness');

      this.status.phase = harnessReady ? 'running' : 'starting';
      if (harnessReady && !this.readyAnnounced) {
        this.readyAnnounced = true;
        this.notify.ready(`ready: proxy + harness on port ${config.harness.port}`);
        this.logger.info('stack-ready', {
          port: config.harness.port,
          tunnel: this.tunnel.url ?? null,
          signIn: this.signIn.url === undefined ? null : 'published',
        });
        this.scheduleVerifyScan();
      }
      if (!harnessReady && this.readyAnnounced) {
        // The stack is up but the UI is down: report it, do not un-ready systemd.
        this.logger.warn('harness-degraded', { why: options.why });
        this.status.phase = 'degraded';
      }
      this.updateStatus();
    } catch (error) {
      this.logger.error('reconcile-failed', { why: options.why, error: renderError(error) });
      this.kick('reconcile-failed');
    } finally {
      this.reconciling = false;
      this.lastProgressAt = Date.now();
    }
  }

  /** Build (or rebuild) component objects from the current configuration. */
  buildComponents() {
    const config = this.config;
    if (config.proxy.mode === 'own' && this.components.proxy === undefined) {
      this.components.proxy = new Component({
        name: 'proxy',
        command: config.proxy.command,
        cwd: config.proxy.cwd,
        env: { ...process.env, ...config.proxy.env },
        logger: this.logger.child('proxy'),
        readyTimeoutMs: config.proxy.readyTimeoutMs,
        ready: () => this.proxyProbe(),
        probe: () => this.proxyProbe(),
      });
    }
    if (config.tunnel.mode === 'own' && this.components.tunnel === undefined) {
      this.components.tunnel = new Component({
        name: 'tunnel',
        command: this.tunnelCommand(),
        cwd: config.harness.cwd,
        env: process.env,
        logger: this.logger.child('tunnel'),
        readyTimeoutMs: config.tunnel.readyTimeoutMs,
        onLine: (line) => this.observeTunnelLine(line),
        ready: async () => {
          if (this.tunnel.host !== undefined) return { ok: true, detail: `tunnel host ${this.tunnel.host}` };
          return { ok: false, detail: 'waiting for a trycloudflare hostname' };
        },
        probe: async () => {
          if (this.tunnel.host === undefined) return { ok: false, detail: 'no hostname yet' };
          return { ok: true, detail: `tunnel host ${this.tunnel.host}` };
        },
      });
    }
    if (config.harness.mode === 'own' && this.components.harness === undefined) {
      this.components.harness = new Component({
        name: 'harness',
        command: this.harnessCommand(),
        cwd: config.harness.cwd,
        env: { ...process.env, ...config.harness.env },
        logger: this.logger.child('harness'),
        readyTimeoutMs: config.harness.readyTimeoutMs,
        onLine: (line) => this.observeHarnessLine(line),
        ready: () => this.harnessProbe(),
        probe: () => this.harnessProbe(),
        onExit: () => this.kick('harness-exited'),
      });
    }
  }

  /**
   * Bring one component to ready, restarting it when it is down, wedged, or
   * has failed enough consecutive probes.
   * @param {'proxy'|'tunnel'|'harness'} name
   * @returns {Promise<boolean>}
   */
  async converge(name) {
    const component = this.components[name];
    if (component === undefined) return false;

    if (component.alive) {
      const probe = await component.check();
      this.lastProgressAt = Date.now();
      if (probe.ok) {
        component.consecutiveFailures = 0;
        component.state = 'ready';
        this.restartState[name].healthySince ??= Date.now();
        return true;
      }
      component.consecutiveFailures += 1;
      this.logger.info('probe-failed', { component: name, detail: probe.detail, failures: component.consecutiveFailures });
      if (component.consecutiveFailures < this.config.health.failureThreshold && component.state === 'ready') {
        return false; // tolerate a single blip; the next pass decides
      }
      await component.stop({ reason: `unhealthy: ${probe.detail}`, graceMs: this.config.restart.graceMs });
    }

    if (!this.admitRestart(name)) return false;

    const delay = this.nextBackoff(name);
    if (delay > 0) await sleep(delay);
    if (this.stopping) return false;

    try {
      if (name === 'harness') {
        component.command = this.harnessCommand();
      }
      await component.start();
      this.restartState[name].healthySince = Date.now();
      this.restartState[name].consecutive = 0;
      this.lastProgressAt = Date.now();
      return true;
    } catch (error) {
      component.lastError = renderError(error);
      this.logger.warn('start-failed', { component: name, error: renderError(error) });
      this.kick(`${name}-start-failed`);
      return false;
    }
  }

  /** @param {'proxy'|'tunnel'|'harness'} name */
  async stopComponent(name, reason, graceMs) {
    const component = this.components[name];
    if (component === undefined) return;
    if (!component.alive) {
      component.state = 'stopped';
      return;
    }
    await component.stop({ reason, graceMs: graceMs ?? this.config.restart.graceMs });
  }

  /** Operator-forced restart of one component (SIGHUP/SIGUSR2). */
  async restartNow(name, reason) {
    if (this.stopping) return;
    try {
      if (name === 'proxy' || name === 'tunnel') await this.stopComponent('harness', `restart ${name}`);
      await this.stopComponent(name, reason);
      this.kick(`restart-${name}`);
    } catch (error) {
      this.logger.warn('restart-failed', { component: name, error: renderError(error) });
    }
  }

  // ------------------------------------------------------------------ probes

  proxyProbe() {
    return httpProbe(this.config.proxy.healthUrl, {
      timeoutMs: this.config.health.timeoutMs,
      expectJsonOk: this.config.proxy.expectJsonOk === true,
    });
  }

  harnessProbe() {
    return httpProbe(this.config.harness.healthUrl, { timeoutMs: this.config.health.timeoutMs });
  }

  /** External proxy: gate on readiness, never restart something we do not own. */
  async probeExternalProxy() {
    const result = await this.proxyProbe();
    this.lastProgressAt = Date.now();
    if (!result.ok) {
      this.logger.warn('external-proxy-down', { detail: result.detail, url: this.config.proxy.healthUrl });
    }
    return result.ok;
  }

  /** @param {string} line */
  observeHarnessLine(line) {
    const match = /token=([A-Za-z0-9_-]+)/.exec(line);
    if (match === null) return;
    this.signIn.token = match[1];
    this.logger.info('sign-in-token', { length: this.signIn.token.length });
    this.publishSignInUrl();
  }

  /** @param {string} line */
  observeTunnelLine(line) {
    const match = /https:\/\/([a-z0-9-]+\.trycloudflare\.com)/.exec(line);
    if (match === null) return;
    const host = match[1];
    if (this.tunnel.host === host) return;
    this.tunnel = { host, url: `https://${host}/`, seenAt: Date.now() };
    this.logger.info('tunnel-host', { host });
    this.publishSignInUrl();
    // The harness needs --trusted-host for this exact authority.
    this.kick('tunnel-host');
  }

  /** Write the sign-in URL next to the legacy supervisor's file. */
  publishSignInUrl() {
    if (this.config.tunnel.publishUrl !== true) return;
    const host = this.tunnel.host;
    const token = this.signIn.token;
    if (token === undefined) return;
    const url = host === undefined
      ? `http://127.0.0.1:${this.config.harness.port}/?token=${token}`
      : `https://${host}/?token=${token}`;
    if (this.signIn.url === url) return;
    this.signIn.url = url;
    this.signIn.published = Date.now();
    try {
      writeFileAtomicSyncText(this.config.urlFile, `${url}\n`);
      this.logger.info('url-published', { file: this.config.urlFile, public: host !== undefined });
    } catch (error) {
      this.logger.warn('url-publish-failed', { file: this.config.urlFile, error: renderError(error) });
    }
  }

  /** The host the Harness should trust, when the tunnel provides one. */
  tunnelTargetHost() {
    if (this.config.tunnel.mode !== 'own') return undefined;
    return this.tunnel.host;
  }

  // -------------------------------------------------------------- commands

  harnessCommand() {
    const config = this.config;
    const argv = [
      config.harness.command,
      'web',
      '--port',
      String(config.harness.port),
      '--no-open',
    ];
    const host = this.harnessTrustedHost ?? this.tunnelTargetHost();
    if (host !== undefined) argv.push('--trusted-host', host);
    for (const extra of config.harness.extraArgs ?? []) argv.push(String(extra));
    return argv;
  }

  tunnelCommand() {
    const argv = [...this.config.tunnel.command];
    const index = argv.findIndex((value) => value.startsWith('http://127.0.0.1'));
    if (index >= 0) argv[index] = `http://127.0.0.1:${this.config.harness.port}`;
    return argv;
  }

  // ------------------------------------------------------------ network gate

  /** Wait for a real outbound connection before touching the proxy. */
  async waitForNetwork() {
    const gate = this.config.network;
    if (gate.enabled !== true) return;
    this.notify.status(`waiting for network (${gate.host}:${gate.port})`);
    const result = await waitUntil(
      async () => {
        const probe = await tcpProbe(gate.host, gate.port, Math.min(gate.timeoutMs, 5_000));
        this.lastProgressAt = Date.now();
        return probe;
      },
      { timeoutMs: gate.timeoutMs, intervalMs: gate.intervalMs },
    );
    if (result.ok) {
      this.logger.info('network-ready', { host: gate.host, port: gate.port, attempts: result.attempts, ms: result.ms });
    } else if (gate.failOpen === true) {
      this.logger.warn('network-timeout', { detail: result.detail, failOpen: true });
    } else {
      this.logger.error('network-unreachable', { detail: result.detail });
    }
  }

  // -------------------------------------------------------------- restarts

  /** @param {'proxy'|'tunnel'|'harness'} name */
  admitRestart(name) {
    const state = this.restartState[name];
    const now = Date.now();
    const window = this.config.restart.windowMs;
    state.history = state.history.filter((at) => now - at < window);
    if (state.history.length >= this.config.restart.maxRestartsPerWindow) {
      void this.fatal(`${name} restarted ${state.history.length} times in ${Math.round(window / 1000)}s`);
      return false;
    }
    return true;
  }

  /** @param {'proxy'|'tunnel'|'harness'} name */
  nextBackoff(name) {
    const state = this.restartState[name];
    const now = Date.now();
    if (state.healthySince !== undefined && now - state.healthySince > this.config.restart.resetAfterMs) {
      state.consecutive = 0;
    }
    const floor = this.config.restart.minRestartIntervalMs;
    const sinceLast = now - state.lastRestartAt;
    const backoff = Math.min(
      this.config.restart.maxBackoffMs,
      this.config.restart.initialBackoffMs * 2 ** Math.min(state.consecutive, 8),
    );
    const jitter = 0.85 + Math.random() * 0.3;
    const delay = Math.max(floor - sinceLast, Math.round(backoff * jitter));
    state.consecutive += 1;
    state.lastRestartAt = now;
    state.history.push(now);
    state.healthySince = undefined;
    return Math.max(0, delay);
  }

  /** Unrecoverable condition: report, then let systemd take over. */
  async fatal(reason) {
    if (this.fatalReason !== undefined || this.stopping) return;
    this.fatalReason = reason;
    this.status.fatal = { reason, at: Date.now() };
    this.logger.error('fatal', { reason });
    this.notify.status(`fatal: ${reason}`);
    try {
      await writeJsonAtomic(path.join(this.config.stateDir, 'status.json'), this.status);
    } catch {
      /* the journald line above is the durable record */
    }
    await this.shutdown(`fatal: ${reason}`, 1);
  }

  // ----------------------------------------------------------------- status

  /** Compose and persist the status document. */
  updateStatus() {
    const now = Date.now();
    const components = {};
    for (const [name, component] of Object.entries(this.components)) {
      if (component !== undefined) components[name] = component.snapshot();
    }
    // Only components that this mode actually manages can gate readiness: in
    // proxy.mode=external there is no proxy component to wait for, and demanding
    // one would keep the unit in `starting` forever.
    const required = [];
    if (this.config.proxy.required !== false && components.proxy !== undefined) required.push('proxy');
    if (this.config.harness.required !== false && components.harness !== undefined) required.push('harness');
    const ready = required.every((name) => components[name]?.state === 'ready' && components[name]?.alive === true);

    this.status.updatedAt = now;
    this.status.ready = ready;
    this.status.components = components;
    this.status.signIn = {
      url: this.signIn.url ?? null,
      host: this.signIn.host ?? null,
      tokenLength: this.signIn.token === undefined ? 0 : this.signIn.token.length,
      publishedAt: this.signIn.published ?? null,
    };
    this.status.tunnel = {
      mode: this.config.tunnel.mode,
      host: this.tunnel.host ?? null,
      url: this.tunnel.url ?? null,
    };
    this.status.port = this.config.harness.port;
    this.status.keeper = readKeeperStatus(this.config.keeper.statusFile);
    this.status.uplink = this.readyAnnounced;
    this.status.lastProgressAt = this.lastProgressAt;
    this.status.phase = this.stopping ? 'stopping' : this.status.phase === 'degraded' && ready ? 'running' : this.status.phase;

    try {
      writeJsonAtomicSync(path.join(this.config.stateDir, 'status.json'), this.status);
    } catch (error) {
      this.logger.warn('status-write-failed', { error: renderError(error) });
    }
    this.notify.status(
      `${this.status.phase}: proxy=${components.proxy?.state ?? 'external'} harness=${components.harness?.state ?? 'off'}`,
    );
  }

  /** One post-boot verification pass: how many sessions still look interrupted. */
  scheduleVerifyScan() {
    if (this.config.keeper.verifyScan !== true) return;
    clearTimeout(this.verifyTimer);
    this.verifyTimer = setTimeout(() => {
      this.verifyTimer = undefined;
      void this.runVerifyScan();
    }, this.config.keeper.verifyScanDelayMs);
    this.verifyTimer.unref?.();
  }

  async runVerifyScan() {
    try {
      const { scanInterrupted } = await import('./sessionscan.mjs');
      const result = await scanInterrupted({ dshHome: this.config.dshHome, limit: 500 });
      this.status.sessions = {
        checkedAt: Date.now(),
        scanned: result.scanned,
        interrupted: result.interrupted.length,
        resumable: result.resumable.length,
        children: result.children,
        errors: result.errors.length,
      };
      this.logger.info('sessions-verified', {
        scanned: result.scanned,
        interrupted: result.interrupted.length,
        resumable: result.resumable.length,
        errors: result.errors.length,
      });
      this.updateStatus();
    } catch (error) {
      this.logger.warn('sessions-verify-failed', { error: renderError(error) });
    }
  }

  // ------------------------------------------------------------------- lock

  /** Refuse to run two supervisors against one stack. */
  acquireInstanceLock() {
    const lockFile = path.join(this.config.stateDir, 'stack.lock');
    const document = { pid: process.pid, bootId: this.bootId, startedAt: this.startedAt, argv: process.argv.slice(0, 4) };
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        fs.mkdirSync(this.config.stateDir, { recursive: true, mode: 0o700 });
        const fd = fs.openSync(lockFile, 'wx', 0o600);
        try {
          fs.writeSync(fd, `${JSON.stringify(document, null, 2)}\n`);
          fs.fsyncSync(fd);
        } finally {
          fs.closeSync(fd);
        }
        fsyncDirSync(this.config.stateDir);
        this.lockFile = lockFile;
        return;
      } catch (error) {
        if (/** @type {any} */ (error)?.code !== 'EEXIST') throw error;
        const holder = readLock(lockFile);
        if (holder !== undefined && processAlive(holder.pid) && looksLikeSupervisor(holder.pid)) {
          this.logger.warn('instance-already-running', { pid: holder.pid, startedAt: holder.startedAt });
          process.exit(0);
        }
        this.logger.warn('stale-lock', { holder: holder ?? null });
        try {
          fs.unlinkSync(lockFile);
        } catch {
          /* another instance won the race */
        }
      }
    }
    throw new Error(`could not acquire ${lockFile}`);
  }

  releaseInstanceLock() {
    if (this.lockFile === undefined) return;
    try {
      fs.unlinkSync(this.lockFile);
    } catch {
      /* already gone */
    }
  }
}

// --------------------------------------------------------------------- utils

function freshRestart() {
  return { lastRestartAt: 0, consecutive: 0, history: [], healthySince: undefined };
}

/** @param {string} file */
function readLock(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return typeof parsed?.pid === 'number' ? parsed : undefined;
  } catch {
    return undefined;
  }
}

/** @param {number} pid */
function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return /** @type {any} */ (error)?.code === 'EPERM';
  }
}

/** Guard against pid reuse: the holder must still be a dsh-stack process. */
function looksLikeSupervisor(pid) {
  try {
    const cmdline = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8');
    return cmdline.includes('dsh-stack');
  } catch {
    return true;
  }
}

/** Read the plugin's status document, tolerating absence and corruption. */
function readKeeperStatus(file) {
  try {
    const raw = fs.readFileSync(file, 'utf8');
    const parsed = JSON.parse(raw);
    return {
      updatedAt: parsed?.updatedAt ?? null,
      pid: parsed?.pid ?? null,
      phase: parsed?.summary?.phase ?? null,
      scanned: parsed?.summary?.scanned ?? null,
      candidates: parsed?.summary?.candidates ?? null,
      resumed: Array.isArray(parsed?.summary?.resumed) ? parsed.summary.resumed.length : null,
      skipped: Array.isArray(parsed?.summary?.skipped) ? parsed.summary.skipped.length : null,
      errors: Array.isArray(parsed?.summary?.errors) ? parsed.summary.errors.slice(0, 3) : [],
    };
  } catch {
    return null;
  }
}

/** Read the kernel boot id (best effort). */
function readBootId() {
  try {
    return fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim();
  } catch {
    return '';
  }
}

/** @param {unknown} error */
export function renderError(error) {
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}

/** Sync atomic text write for the URL file (also used by tests). */
export function writeFileAtomicSyncText(file, text) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  const fd = fs.openSync(temp, 'w', 0o600);
  try {
    fs.writeSync(fd, text);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temp, file);
  fsyncDirSync(dir);
}

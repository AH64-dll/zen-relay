/**
 * The recovery engine: find sessions whose last turn never closed, and resume
 * them exactly once, in order, behind a readiness gate.
 *
 * Boundaries this module respects:
 *
 * - **Log is truth.** It never trusts a journal over the session log; a turned
 *   closed by a successful resume is not resumed again by construction.
 * - **Ownership is the mutex.** `agents.resume()` takes the persistence write
 *   handle (an OS-level flock), so a session owned by another live harness
 *   process fails closed and is skipped rather than forked.
 * - **Never touch a live session.** In-process agents/sessions are checked
 *   first, which also makes a plugin HMR reload harmless.
 * - **Never resume into a dead route.** Recovery waits for the model proxy to
 *   answer `/healthz` before spending an attempt.
 *
 * @module dsh-session-keeper/keeper
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { randomUUID } from 'node:crypto';

import { analyze, buildRecoveryPrompt, describe, isEligibleHeader } from './events.mjs';
import { LEDGER_DEFAULTS, RecoveryLedger, fsyncDir } from './ledger.mjs';

/** Tunable defaults; every field can be overridden from the plugin row's config. */
export const DEFAULTS = Object.freeze({
  /** Master switch: register the engine at all. */
  enabled: true,
  /** Recover automatically on start (false = only via `/keeper recover`). */
  autoRecover: true,
  /** Analyse and decide, but never call `agents.resume()`. */
  dryRun: false,
  /** Let the rest of the host finish mounting before scanning. */
  settleMs: 6000,
  /** Directory for the ledger, status document and log. */
  stateDir: undefined,
  /** Retained for symmetry with the daemon; the plugin derives URLs itself. */
  proxyHealthUrl: 'http://127.0.0.1:8901/healthz',
  /** Give up recovery for this boot if the model route never becomes ready. */
  proxyWaitMs: 10 * 60 * 1000,
  /** Do not even try to recover when the route is down. */
  requireProxy: true,
  /** First tail window, in events. */
  initialWindowEvents: 512,
  /** Largest tail window before falling back to a full read. */
  maxWindowEvents: 8192,
  /** Skip session logs larger than this. */
  maxScanBytes: 32 * 1024 * 1024,
  /** Upper bound on sessions inspected per run. */
  maxScanSessions: 500,
  /** Sessions resumed concurrently. */
  maxConcurrent: 2,
  /** Sessions resumed per boot/run. */
  maxResumesPerBoot: 5,
  /**
   * Automatic recovery ignores sessions whose last event is older than this:
   * a crash leaves a *recent* tail, a session idle for days was abandoned on
   * purpose. `0` disables the gate; manual `/keeper recover` bypasses it.
   */
  maxAgeMs: 24 * 60 * 60 * 1000,
  /** Include subagent child sessions (normally owned by their parent). */
  includeSubagents: false,
  /** Restrict recovery to these working directories (empty = all). */
  cwds: [],
  /** Ask an in-flight turn to stop before the process would be restarted. */
  ledger: {},
});

/**
 * @typedef {object} RunSummary
 * @property {number} startedAt
 * @property {number} [finishedAt]
 * @property {'idle'|'waiting'|'scanning'|'resuming'|'done'|'blocked'|'disabled'} phase
 * @property {number} scanned
 * @property {number} candidates
 * @property {Array<Record<string, unknown>>} resumed
 * @property {Array<Record<string, unknown>>} skipped
 * @property {string[]} errors
 * @property {boolean} dryRun
 */

/** Resolve `$DSH_HOME` without importing harness internals. */
function dshHome() {
  return process.env.DSH_HOME && process.env.DSH_HOME.length > 0 ? process.env.DSH_HOME : path.join(os.homedir(), '.dsh');
}

/**
 * Merge user config over defaults, clamping the values that could hurt.
 * @param {Record<string, unknown>|undefined} raw
 * @param {{stateDir?: string}} [env]
 */
export function normalizeConfig(raw, env = {}) {
  const input = raw !== null && typeof raw === 'object' ? raw : {};
  const num = (value, fallback, min, max) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(max, Math.max(min, parsed));
  };
  const bool = (value, fallback) => (typeof value === 'boolean' ? value : fallback);
  // Fleet-wide kill switch: a second Host (shadow port, staging profile) must
  // never resume a session the primary Host is still serving.
  const disabledByEnv = env.disabled === true || process.env.DSH_SESSION_KEEPER_DISABLED === '1';
  const stateDir = typeof input.stateDir === 'string' && input.stateDir.length > 0
    ? input.stateDir
    : (env.stateDir ?? path.join(dshHome(), 'session-keeper'));

  return {
    ...DEFAULTS,
    ...input,
    enabled: disabledByEnv ? false : bool(input.enabled, DEFAULTS.enabled),
    autoRecover: bool(input.autoRecover, DEFAULTS.autoRecover),
    dryRun: bool(input.dryRun, DEFAULTS.dryRun),
    requireProxy: bool(input.requireProxy, DEFAULTS.requireProxy),
    includeSubagents: bool(input.includeSubagents, DEFAULTS.includeSubagents),
    settleMs: num(input.settleMs, DEFAULTS.settleMs, 0, 5 * 60 * 1000),
    proxyWaitMs: num(input.proxyWaitMs, DEFAULTS.proxyWaitMs, 0, 60 * 60 * 1000),
    initialWindowEvents: num(input.initialWindowEvents, DEFAULTS.initialWindowEvents, 32, 100_000),
    maxWindowEvents: num(input.maxWindowEvents, DEFAULTS.maxWindowEvents, 32, 1_000_000),
    maxScanBytes: num(input.maxScanBytes, DEFAULTS.maxScanBytes, 1024, 512 * 1024 * 1024),
    maxScanSessions: num(input.maxScanSessions, DEFAULTS.maxScanSessions, 1, 100_000),
    maxConcurrent: num(input.maxConcurrent, DEFAULTS.maxConcurrent, 1, 8),
    maxResumesPerBoot: num(input.maxResumesPerBoot, DEFAULTS.maxResumesPerBoot, 0, 100),
    maxAgeMs: num(input.maxAgeMs, DEFAULTS.maxAgeMs, 0, 365 * 24 * 60 * 60 * 1000),
    cwds: Array.isArray(input.cwds) ? input.cwds.filter((value) => typeof value === 'string' && value.length > 0) : [],
    stateDir,
    ledgerFile: typeof input.ledgerFile === 'string' && input.ledgerFile.length > 0 ? input.ledgerFile : path.join(stateDir, 'ledger.json'),
    statusFile: typeof input.statusFile === 'string' && input.statusFile.length > 0 ? input.statusFile : path.join(stateDir, 'status.json'),
    logFile: typeof input.logFile === 'string' && input.logFile.length > 0 ? input.logFile : path.join(stateDir, 'keeper.log'),
    ledger: { ...LEDGER_DEFAULTS, ...(input.ledger !== null && typeof input.ledger === 'object' ? input.ledger : {}) },
  };
}

/**
 * One read of a message's text content, without depending on harness packages.
 * @param {unknown} message
 * @returns {string}
 */
export { analyze };

/**
 * The session keeper. One instance per Host process.
 */
export class SessionKeeper {
  /**
   * @param {import('@deepseek-ai/cordis').Context} ctx
   * @param {Record<string, unknown>|undefined} rawConfig
   */
  constructor(ctx, rawConfig) {
    this.ctx = ctx;
    this.config = normalizeConfig(rawConfig);
    /** @type {any} */
    this.logger = resolveLogger(ctx);
    this.ledger = new RecoveryLedger({
      file: this.config.ledgerFile,
      config: this.config.ledger,
      logger: this.logger,
    });
    /** @type {Map<string, any>} handles of agents this engine resumed, kept alive */
    this.handles = new Map();
    /** @type {RunSummary} */
    this.summary = {
      startedAt: Date.now(),
      phase: 'idle',
      scanned: 0,
      candidates: 0,
      resumed: [],
      skipped: [],
      errors: [],
      dryRun: this.config.dryRun,
    };
    this.startedAt = Date.now();
    this.bootId = readBootId();
    this.stopped = false;
    this.running = false;
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    this.timer = undefined;
    this.ensureStateDir();
  }

  /** Create the state directory up front so status/log writes never race. */
  ensureStateDir() {
    try {
      fs.mkdirSync(this.config.stateDir, { recursive: true, mode: 0o700 });
    } catch (error) {
      this.note('warn', `state dir unavailable (${/** @type {Error} */ (error).message}); recovery will still run`);
    }
  }

  /**
   * Schedule the boot run and return a disposer.
   * @returns {() => void}
   */
  start() {
    if (!this.config.enabled) {
      this.summary.phase = 'disabled';
      this.writeStatus();
      this.note('info', 'disabled by config');
      return () => {};
    }
    if (!this.config.autoRecover) {
      this.summary.phase = 'idle';
      this.writeStatus();
      this.note('info', 'auto recovery disabled; use /keeper recover');
      return () => {};
    }
    this.writeStatus();
    this.note('info', `armed; scanning in ${this.config.settleMs}ms (dryRun=${this.config.dryRun})`);
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.runOnce({ trigger: 'boot' }).catch((error) => {
        this.note('warn', `boot recovery crashed: ${renderError(error)}`);
      });
    }, this.config.settleMs);
    // Never keep the host alive just for this timer.
    this.timer.unref?.();
    return () => {
      this.stopped = true;
      if (this.timer !== undefined) clearTimeout(this.timer);
      this.timer = undefined;
    };
  }

  /**
   * One full recovery pass: gate, scan, decide, resume.
   * @param {{trigger?: 'boot'|'manual'|'command', dryRun?: boolean}} [options]
   * @returns {Promise<RunSummary>}
   */
  async runOnce(options = {}) {
    if (this.stopped) return this.summary;
    if (this.running) {
      this.note('info', 'recovery pass already running; ignoring duplicate request');
      return this.summary;
    }
    this.running = true;
    const dryRun = options.dryRun ?? this.config.dryRun;
    this.summary = {
      startedAt: Date.now(),
      phase: 'waiting',
      scanned: 0,
      candidates: 0,
      resumed: [],
      skipped: [],
      errors: [],
      dryRun,
    };
    this.writeStatus();
    try {
      const persistence = this.ctx.get('sessionPersistence');
      if (persistence === undefined) {
        this.summary.phase = 'blocked';
        this.summary.errors.push('sessionPersistence service is not mounted');
        this.note('warn', 'no session persistence in this host; nothing to recover');
        return this.summary;
      }

      if (this.config.requireProxy) {
        const gate = await this.waitForProxy();
        if (!gate.ok) {
          this.summary.phase = 'blocked';
          this.summary.errors.push(`model route not ready: ${gate.detail}`);
          this.note('warn', `recovery deferred: ${gate.detail}`);
          return this.summary;
        }
      }

      this.summary.phase = 'scanning';
      this.writeStatus();
      const snapshots = await persistence.list();
      const list = snapshots
        .filter((snapshot) => snapshot?.header?.id !== undefined)
        .sort((a, b) => (b.header.createdAt ?? 0) - (a.header.createdAt ?? 0))
        .slice(0, this.config.maxScanSessions);

      /** @type {Array<{id: string, header: any, analysis: any}>} */
      const candidates = [];
      for (const snapshot of list) {
        this.summary.scanned += 1;
        const verdict = this.classify(snapshot);
        if (!verdict.eligible) {
          this.skip(snapshot.header.id, undefined, verdict.reason);
          continue;
        }
        const inspected = await this.inspect(persistence, snapshot);
        if (inspected.analysis === undefined) {
          this.skip(snapshot.header.id, undefined, inspected.reason ?? 'inspect-failed');
          if (inspected.error !== undefined) this.summary.errors.push(`${snapshot.header.id}: ${renderError(inspected.error)}`);
          continue;
        }
        if (!inspected.analysis.open) continue;
        candidates.push({ id: snapshot.header.id, header: snapshot.header, analysis: inspected.analysis });
      }

      this.summary.candidates = candidates.length;
      this.note('info', `scan complete: ${this.summary.scanned} sessions, ${candidates.length} interrupted`);
      if (candidates.length === 0) {
        this.summary.phase = 'done';
        return this.summary;
      }

      this.summary.phase = 'resuming';
      this.writeStatus();
      const limit = Math.min(candidates.length, this.config.maxResumesPerBoot);
      if (candidates.length > limit) {
        this.note('warn', `only the ${limit} most recent of ${candidates.length} interrupted sessions will be resumed this boot`);
      }
      const automatic = options.trigger === undefined || options.trigger === 'boot';
      await this.mapLimit(candidates.slice(0, limit), this.config.maxConcurrent, async (candidate) => {
        if (this.stopped) return;
        await this.resume(candidate, dryRun, { automatic });
      });

      this.summary.phase = 'done';
      return this.summary;
    } catch (error) {
      this.summary.phase = 'blocked';
      this.summary.errors.push(renderError(error));
      this.note('warn', `recovery pass failed: ${renderError(error)}`);
      return this.summary;
    } finally {
      this.summary.finishedAt = Date.now();
      this.running = false;
      this.ledger.prune();
      this.writeStatus();
    }
  }

  /**
   * Cheap eligibility checks before reading anything.
   * @param {{header: any, sizeBytes?: number}} snapshot
   */
  classify(snapshot) {
    const header = snapshot.header;
    const id = header.id;
    if (this.ctx.get('agents')?.get?.(id) !== undefined) return { eligible: false, reason: 'live-in-this-host' };
    if (this.ctx.get('sessions')?.get?.(id) !== undefined) return { eligible: false, reason: 'attached-in-this-host' };
    const eligibility = isEligibleHeader(header, this.config);
    if (!eligibility.eligible) return eligibility;
    if (typeof snapshot.sizeBytes === 'number' && snapshot.sizeBytes > this.config.maxScanBytes) {
      return { eligible: false, reason: 'log-too-large' };
    }
    if (typeof header.cwd === 'string' && header.cwd.length > 0) {
      try {
        if (!fs.statSync(header.cwd).isDirectory()) return { eligible: false, reason: 'workspace-missing' };
      } catch {
        return { eligible: false, reason: 'workspace-missing' };
      }
    }
    return { eligible: true };
  }

  /**
   * Read the tail of one stored session and decide whether its final turn is open.
   * @param {any} persistence
   * @param {{header: any, eventCount?: number}} snapshot
   */
  async inspect(persistence, snapshot) {
    const id = snapshot.header.id;
    /** @type {any} */
    let handle;
    try {
      handle = await persistence.open(id, 'read');
    } catch (error) {
      return { reason: `open-failed:${codeOf(error)}`, error };
    }
    try {
      const total = snapshot.eventCount;
      if (typeof total !== 'number') {
        const { events } = await handle.read();
        if (events.length === 0) return { reason: 'empty-log' };
        return { analysis: analyze(events, { atLogStart: true }), total: events.length };
      }
      if (total === 0) return { reason: 'empty-log' };

      let window = Math.min(this.config.initialWindowEvents, total);
      for (;;) {
        const start = Math.max(0, total - window);
        const { events } = await handle.read(start, window);
        const analysis = analyze(events, { atLogStart: start === 0 });
        analysis.totalEvents = total;
        analysis.windowStart = start;
        if (analysis.complete) return { analysis, total };
        if (start === 0) return { analysis, total };
        if (window >= this.config.maxWindowEvents) break;
        window = Math.min(window * 4, this.config.maxWindowEvents);
      }

      // Inconclusive tail: fall back to one full read rather than guessing.
      const { events } = await handle.read();
      const analysis = analyze(events, { atLogStart: true });
      analysis.totalEvents = events.length;
      analysis.windowStart = 0;
      return { analysis, total: events.length };
    } catch (error) {
      return { reason: `read-failed:${codeOf(error)}`, error };
    } finally {
      try {
        await handle.close();
      } catch {
        /* closing a read handle is idempotent best-effort */
      }
    }
  }

  /**
   * Decide and perform one resume.
   * @param {{id: string, header: any, analysis: any}} candidate
   * @param {boolean} dryRun
   * @param {{automatic?: boolean}} [options]
   */
  async resume(candidate, dryRun, options = {}) {
    const { id, header, analysis } = candidate;
    const turnSeq = analysis.turnStartSeq ?? 0;
    const now = Date.now();

    // A crash leaves a recent tail. An open turn from days ago is far more
    // likely to be abandoned work, and resuming it would spend the user's
    // tokens on their behalf. Manual `/keeper recover` bypasses this.
    if (options.automatic === true && this.config.maxAgeMs > 0 && typeof analysis.lastEventTime === 'number') {
      const age = now - analysis.lastEventTime;
      if (age > this.config.maxAgeMs) {
        this.skip(id, turnSeq, `session-too-old (${Math.round(age / 3_600_000)}h old)`);
        return;
      }
    }

    const plan = this.ledger.decide(id, turnSeq, now);
    if (!plan.allow) {
      this.skip(id, turnSeq, plan.reason);
      if (plan.reason === 'session-attempt-limit') {
        this.ledger.quarantineSession(id, plan.reason, now);
        this.note('warn', `session ${id} quarantined: ${plan.reason}`);
      } else if (plan.reason === 'turn-attempt-limit' || plan.reason === 'unresolved-intent-limit') {
        this.ledger.quarantineTurn(id, turnSeq, plan.reason, now);
      }
      return;
    }

    if (dryRun) {
      this.record('dry-run', id, turnSeq, { reason: plan.reason, attempts: plan.attempts });
      this.note('info', `[dry-run] would resume ${id} turn ${analysis.turn} (${describe(analysis)})`);
      return;
    }

    // Durable intent first: a crash inside resume() must be visible next boot.
    this.ledger.begin(id, turnSeq, plan, now);
    const agents = this.ctx.get('agents');
    if (agents === undefined) {
      this.ledger.fail(id, turnSeq, new Error('agents service is not mounted'));
      this.summary.errors.push('agents service is not mounted');
      return;
    }

    /** @type {any} */
    let handle;
    try {
      const setup = await this.composeSetup(header);
      handle = await agents.resume({
        resumeSessionId: id,
        ...(setup === undefined ? {} : { setup }),
        ...this.agentOptions(),
      });
      this.ledger.confirmStarted(id, turnSeq, Date.now());
    } catch (error) {
      const name = /** @type {any} */ (error)?.name ?? '';
      if (name === 'SessionAlreadyOwnedError' || name === 'SessionHandleClosedError') {
        // Another live process owns the write handle: correctness requires
        // backing off, not competing.
        this.ledger.skip(id, turnSeq, 'writer-held-elsewhere');
        this.skip(id, turnSeq, 'writer-held-elsewhere');
        return;
      }
      this.ledger.fail(id, turnSeq, error);
      this.skip(id, turnSeq, `resume-failed:${codeOf(error)}`);
      this.note('warn', `resume failed for ${id}: ${renderError(error)}`);
      return;
    }

    try {
      const prompt = buildRecoveryPrompt(analysis, {
        sessionId: id,
        cwd: header.cwd,
        attempt: plan.attempts,
        maxAttempts: this.config.ledger.maxAttemptsPerTurn ?? 1,
        interruptedAt: Date.now(),
        host: os.hostname(),
      });
      handle.agent.followup({
        id: randomUUID(),
        role: 'user',
        source: { kind: 'user' },
        content: [{ type: 'text', text: prompt }],
      });
      this.ledger.confirmResumed(id, turnSeq, Date.now());
      this.handles.set(id, handle);
      this.record('resumed', id, turnSeq, {
        turn: analysis.turn,
        attempts: plan.attempts,
        cwd: header.cwd,
        events: analysis.totalEvents,
      });
      this.note('info', `resumed ${id} turn ${analysis.turn} (${describe(analysis)})`);
    } catch (error) {
      this.ledger.fail(id, turnSeq, error);
      this.skip(id, turnSeq, `followup-failed:${codeOf(error)}`);
      this.note('warn', `recovery prompt rejected for ${id}: ${renderError(error)}`);
      try {
        await handle.dispose();
      } catch {
        /* the handle may already be gone */
      }
    }
  }

  /**
   * Rebuild the agent's preset world so a resumed session keeps its tools and
   * prompt sections. Mirrors what the Web host does when a user opens a
   * session; a missing preset registry simply yields no setup.
   * @param {any} header
   */
  async composeSetup(header) {
    const presets = this.ctx.get('agentPresets');
    if (presets === undefined) return undefined;
    try {
      const resolved = await presets.resolve(header?.agentPreset);
      return async (agentCtx) => {
        await presets.mount(agentCtx, resolved.id);
      };
    } catch (error) {
      this.note('warn', `preset ${String(header?.agentPreset)} unavailable (${codeOf(error)}); resuming without it`);
      return undefined;
    }
  }

  /** The route/model the agent should start on, when the host exposes a default. */
  agentOptions() {
    const model = this.ctx.get('agentDefaultModel');
    if (model === undefined) return {};
    try {
      const selection = model.currentSelection();
      if (selection?.provider && selection?.model) {
        return { agentOptions: { provider: selection.provider, model: selection.model } };
      }
    } catch {
      /* fall through to the session's own defaults */
    }
    return {};
  }

  /**
   * Wait for the model proxy to answer, with bounded backoff.
   * @returns {Promise<{ok: boolean, detail: string}>}
   */
  async waitForProxy() {
    const started = Date.now();
    const deadline = started + this.config.proxyWaitMs;
    let attempt = 0;
    let last = 'not probed';
    for (;;) {
      attempt += 1;
      const probe = await probeUrl(this.config.proxyHealthUrl, 4000);
      if (probe.ok) {
        this.note('info', `model route ready after ${attempt} probe(s)`);
        return { ok: true, detail: 'ready' };
      }
      last = probe.detail;
      if (this.stopped) return { ok: false, detail: 'host stopped' };
      if (Date.now() >= deadline) return { ok: false, detail: last };
      const wait = Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5));
      this.note('info', `model route not ready (${last}); retrying in ${wait}ms`);
      await sleep(wait);
    }
  }

  /**
   * Run `worker` over `items` with bounded concurrency, preserving order.
   * @template T
   * @param {T[]} items
   * @param {number} limit
   * @param {(item: T) => Promise<void>} worker
   */
  async mapLimit(items, limit, worker) {
    let index = 0;
    const runners = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
      for (;;) {
        const current = index++;
        if (current >= items.length) return;
        await worker(items[current]);
      }
    });
    await Promise.all(runners);
  }

  /** @param {string} id @param {number|undefined} turnSeq @param {string} reason */
  skip(id, turnSeq, reason) {
    this.summary.skipped.push({ sessionId: id, turnSeq, reason });
    this.note('info', `skip ${id}${turnSeq === undefined ? '' : ` turn@${turnSeq}`}: ${reason}`);
  }

  /** @param {'resumed'|'dry-run'} status @param {string} id @param {number} turnSeq @param {Record<string, unknown>} extra */
  record(status, id, turnSeq, extra) {
    this.summary.resumed.push({ status, sessionId: id, turnSeq, at: Date.now(), ...extra });
  }

  /** Persist the status document read by `dsh-stack` and `/keeper status`. */
  writeStatus() {
    const document = {
      version: 1,
      pid: process.pid,
      bootId: this.bootId,
      startedAt: this.startedAt,
      updatedAt: Date.now(),
      config: {
        enabled: this.config.enabled,
        autoRecover: this.config.autoRecover,
        dryRun: this.config.dryRun,
        maxConcurrent: this.config.maxConcurrent,
        maxResumesPerBoot: this.config.maxResumesPerBoot,
        maxAgeMs: this.config.maxAgeMs,
        includeSubagents: this.config.includeSubagents,
        cwds: this.config.cwds,
      },
      host: {
        hostname: os.hostname(),
        uptimeSec: Math.round(process.uptime()),
        dshProfile: process.env.DSH_PROFILE,
      },
      summary: this.summary,
      ledger: this.safeLedgerSnapshot(),
    };
    writeJsonAtomic(this.config.statusFile, document);
  }

  safeLedgerSnapshot() {
    try {
      return this.ledger.snapshot();
    } catch {
      return { entries: 0, sessions: 0, recent: [] };
    }
  }

  /**
   * Append one structured line to the keeper log and mirror it to the Host log.
   * @param {'info'|'warn'} level
   * @param {string} message
   */
  note(level, message) {
    const line = JSON.stringify({ ts: new Date().toISOString(), level, msg: message }) + '\n';
    try {
      fs.mkdirSync(this.config.stateDir, { recursive: true, mode: 0o700 });
      const fd = fs.openSync(this.config.logFile, 'a', 0o600);
      try {
        fs.writeSync(fd, line);
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      /* the Host log below is the fallback */
    }
    try {
      this.logger?.[level]?.(`session-keeper: ${message}`);
    } catch {
      /* logging must never break recovery */
    }
  }
}

/** Resolve the Cordis logger across its two shapes. */
function resolveLogger(ctx) {
  const candidate = /** @type {any} */ (ctx).logger;
  if (candidate === undefined) return undefined;
  if (typeof candidate?.info === 'function' && typeof candidate?.warn === 'function') return candidate;
  if (typeof candidate === 'function') {
    try {
      return candidate('dsh-session-keeper');
    } catch {
      return undefined;
    }
  }
  return undefined;
}

/** @param {number} ms */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Read the kernel boot id ('' when unavailable). */
function readBootId() {
  try {
    return fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim();
  } catch {
    return '';
  }
}

/** @param {unknown} error */
function renderError(error) {
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}

/** @param {unknown} error */
function codeOf(error) {
  const code = /** @type {any} */ (error)?.code;
  if (typeof code === 'string' && code.length > 0) return code;
  return /** @type {any} */ (error)?.name ?? 'error';
}

/**
 * Minimal HTTP GET used for the readiness gate. Deliberately dependency-free:
 * the plugin must not depend on harness packages to be resolvable.
 * @param {string} url
 * @param {number} timeoutMs
 * @returns {Promise<{ok: boolean, status?: number, detail: string}>}
 */
async function probeUrl(url, timeoutMs) {
  const { request } = await import('node:http');
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    try {
      const req = request(url, { method: 'GET', timeout: timeoutMs, headers: { 'user-agent': 'dsh-session-keeper' } }, (response) => {
        response.resume();
        const status = response.statusCode ?? 0;
        finish({ ok: status >= 200 && status < 500, status, detail: `HTTP ${status}` });
      });
      req.on('timeout', () => {
        req.destroy();
        finish({ ok: false, detail: `timeout after ${timeoutMs}ms` });
      });
      req.on('error', (error) => finish({ ok: false, detail: `${error.code ?? error.message}` }));
      req.end();
    } catch (error) {
      finish({ ok: false, detail: /** @type {Error} */ (error).message });
    }
  });
}

/**
 * Atomic JSON write: temp file, fsync, rename, directory fsync.
 * @param {string} file
 * @param {unknown} value
 */
export function writeJsonAtomic(file, value) {
  const dir = path.dirname(file);
  try {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const temp = `${file}.${process.pid}.tmp`;
    const fd = fs.openSync(temp, 'w', 0o600);
    try {
      fs.writeSync(fd, `${JSON.stringify(value, null, 2)}\n`);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(temp, file);
    fsyncDir(dir);
    return true;
  } catch {
    return false;
  }
}

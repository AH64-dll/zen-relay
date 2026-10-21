/**
 * Durable recovery ledger: the idempotency and crash-loop guard for automatic
 * session resumption.
 *
 * The session log is the authoritative record of *what* was interrupted; this
 * ledger records *what we already tried to do about it*. Keeping the two apart
 * is what makes recovery safe after a crash at any point:
 *
 * - the ledger is written **before** a resume is attempted (`intent`), so a
 *   crash inside `agents.resume()` is visible on the next boot;
 * - an `intent` that never reached `started` is retried without spending an
 *   attempt, but only a bounded number of times, so a crash loop cannot spin
 *   forever;
 * - every confirmed resume spends one attempt for that exact `(session, turn)`
 *   and one for the session inside a rolling window, which bounds recovery
 *   storms after a poison-pill session that keeps killing the host.
 *
 * The store is a single JSON document replaced atomically (temp + fsync +
 * rename + directory fsync). A corrupt or unreadable document is quarantined
 * and treated as empty: losing the ledger costs one extra recovery attempt,
 * never correctness, because the log predicate still prevents re-running a
 * turn that was already resumed.
 *
 * @module dsh-session-keeper/ledger
 */

import fs from 'node:fs';
import path from 'node:path';

const VERSION = 1;

/** Defaults for every bound the ledger enforces. */
export const LEDGER_DEFAULTS = Object.freeze({
  /** Confirmed resumes allowed for one exact `(session, turn)` key. */
  maxAttemptsPerTurn: 1,
  /** Retries allowed for an intent that never confirmed `started`. */
  maxUnresolvedIntents: 3,
  /** Confirmed resumes allowed for one session inside `sessionWindowMs`. */
  maxAttemptsPerSession: 3,
  /** Rolling window for the per-session cap. */
  sessionWindowMs: 6 * 60 * 60 * 1000,
  /** How long a quarantined session (or turn) is skipped. */
  quarantineMs: 24 * 60 * 60 * 1000,
  /** Entries older than this are pruned. */
  retentionMs: 7 * 24 * 60 * 60 * 1000,
  /** Hard cap on retained entries. */
  maxEntries: 5000,
  /** A lock file older than this is considered abandoned. */
  lockStaleMs: 30_000,
});

/**
 * @typedef {'intent'|'started'|'resumed'|'failed'|'skipped'|'quarantined'|'denied'} Phase
 *
 * @typedef {object} Entry
 * @property {string} sessionId
 * @property {number} turnSeq
 * @property {Phase} phase
 * @property {number} attempts confirmed resumes
 * @property {number} unresolvedIntents intents that never reached `started`
 * @property {number} firstAt
 * @property {number} updatedAt
 * @property {string} [lastError]
 * @property {number} [until] quarantine deadline for this key
 *
 * @typedef {object} SessionEntry
 * @property {Phase} phase
 * @property {number} until
 * @property {string} [reason]
 * @property {number} updatedAt
 */

/** Key for one `(session, turn)` recovery unit. */
export function turnKey(sessionId, turnSeq) {
  return `${sessionId}#${turnSeq}`;
}

/** Key for the session-level quarantine record. */
export function sessionKey(sessionId) {
  return `session:${sessionId}`;
}

/**
 * Decide whether one recovery attempt may start. Pure.
 *
 * @param {{entry?: Entry, session?: SessionEntry, sessionAttempts: number, now: number, config: typeof LEDGER_DEFAULTS}} input
 * @returns {{allow: boolean, reason: string, attempts: number, unresolvedIntents: number, spend: 'attempt'|'intent'|'none'}}
 */
export function planAttempt(input) {
  const { entry, session } = input;
  const now = input.now;
  const config = { ...LEDGER_DEFAULTS, ...input.config };

  if (session !== undefined && session.until > now) {
    return { allow: false, reason: `session-quarantined:${session.reason ?? 'unknown'}`, attempts: entry?.attempts ?? 0, unresolvedIntents: entry?.unresolvedIntents ?? 0, spend: 'none' };
  }
  if (entry !== undefined && entry.until !== undefined && entry.until > now) {
    return { allow: false, reason: 'turn-quarantined', attempts: entry.attempts, unresolvedIntents: entry.unresolvedIntents, spend: 'none' };
  }
  if (entry !== undefined && (entry.phase === 'resumed' || entry.phase === 'started')) {
    return { allow: false, reason: `already-${entry.phase}`, attempts: entry.attempts, unresolvedIntents: entry.unresolvedIntents, spend: 'none' };
  }

  if (entry !== undefined && entry.phase === 'intent') {
    const unresolvedIntents = entry.unresolvedIntents + 1;
    if (unresolvedIntents > config.maxUnresolvedIntents) {
      return { allow: false, reason: 'unresolved-intent-limit', attempts: entry.attempts, unresolvedIntents, spend: 'none' };
    }
    return { allow: true, reason: 'retry-unconfirmed-intent', attempts: entry.attempts, unresolvedIntents, spend: 'intent' };
  }

  const attempts = (entry?.attempts ?? 0) + 1;
  if (attempts > config.maxAttemptsPerTurn) {
    return { allow: false, reason: 'turn-attempt-limit', attempts, unresolvedIntents: entry?.unresolvedIntents ?? 0, spend: 'none' };
  }
  if (input.sessionAttempts + 1 > config.maxAttemptsPerSession) {
    return { allow: false, reason: 'session-attempt-limit', attempts, unresolvedIntents: entry?.unresolvedIntents ?? 0, spend: 'none' };
  }
  return { allow: true, reason: 'fresh-attempt', attempts, unresolvedIntents: entry?.unresolvedIntents ?? 0, spend: 'attempt' };
}

/** Count confirmed attempts for a session inside the rolling window. */
function sessionAttemptsInside(entries, sessionId, now, windowMs) {
  let count = 0;
  for (const entry of Object.values(entries)) {
    if (entry.sessionId !== sessionId) continue;
    if (!(entry.phase === 'started' || entry.phase === 'resumed' || entry.phase === 'failed')) continue;
    if (now - entry.updatedAt > windowMs) continue;
    count += entry.attempts > 0 ? entry.attempts : 1;
  }
  return count;
}

/**
 * Durable, crash-safe ledger.
 */
export class RecoveryLedger {
  /**
   * @param {{file: string, config?: Partial<typeof LEDGER_DEFAULTS>, logger?: {info?: Function, warn?: Function}}} options
   */
  constructor(options) {
    this.file = options.file;
    this.dir = path.dirname(options.file);
    this.lockFile = `${options.file}.lock`;
    this.config = { ...LEDGER_DEFAULTS, ...(options.config ?? {}) };
    this.logger = options.logger;
    /** @type {{version: number, updatedAt: number, entries: Record<string, Entry>, sessions: Record<string, SessionEntry>}} */
    this.state = { version: VERSION, updatedAt: 0, entries: {}, sessions: {} };
    this.loaded = false;
  }

  /** Read the ledger, quarantining a corrupt document instead of failing. */
  load() {
    if (this.loaded) return this.state;
    this.loaded = true;
    let raw;
    try {
      raw = fs.readFileSync(this.file, 'utf8');
    } catch (error) {
      if (/** @type {any} */ (error)?.code !== 'ENOENT') {
        this.note('warn', `ledger unreadable (${/** @type {any} */ (error)?.code ?? 'error'}); starting empty`);
      }
      return this.state;
    }
    try {
      const parsed = JSON.parse(raw);
      if (parsed === null || typeof parsed !== 'object' || parsed.version !== VERSION) throw new Error('unsupported ledger document');
      this.state = {
        version: VERSION,
        updatedAt: Number(parsed.updatedAt) || 0,
        entries: sanitizeEntries(parsed.entries),
        sessions: sanitizeSessions(parsed.sessions),
      };
    } catch (error) {
      const quarantine = `${this.file}.corrupt-${Date.now()}`;
      try {
        fs.renameSync(this.file, quarantine);
      } catch {
        /* keep going: an unreadable ledger must never block recovery */
      }
      this.note('warn', `ledger corrupt (${/** @type {Error} */ (error).message}); quarantined to ${path.basename(quarantine)} and reset`);
      this.state = { version: VERSION, updatedAt: 0, entries: {}, sessions: {} };
    }
    return this.state;
  }

  /** @returns {Record<string, Entry>} */
  entries() {
    return this.load().entries;
  }

  /** @returns {Record<string, SessionEntry>} */
  sessions() {
    return this.load().sessions;
  }

  /**
   * Decide whether this `(session, turn)` may be resumed right now.
   * @param {string} sessionId
   * @param {number} turnSeq
   * @param {number} now
   */
  decide(sessionId, turnSeq, now = Date.now()) {
    const { entries, sessions } = this.load();
    return planAttempt({
      entry: entries[turnKey(sessionId, turnSeq)],
      session: sessions[sessionKey(sessionId)],
      sessionAttempts: sessionAttemptsInside(entries, sessionId, now, this.config.sessionWindowMs),
      now,
      config: this.config,
    });
  }

  /**
   * Record the intent to resume, before the resume call. Durable when this
   * returns, so a crash inside the resume is never mistaken for "never tried".
   * @param {string} sessionId
   * @param {number} turnSeq
   * @param {ReturnType<typeof planAttempt>} plan
   * @param {number} now
   */
  begin(sessionId, turnSeq, plan, now = Date.now()) {
    const key = turnKey(sessionId, turnSeq);
    const previous = this.load().entries[key];
    this.load().entries[key] = {
      sessionId,
      turnSeq,
      phase: 'intent',
      attempts: plan.spend === 'attempt' ? plan.attempts : (previous?.attempts ?? 0),
      unresolvedIntents: plan.unresolvedIntents,
      firstAt: previous?.firstAt ?? now,
      updatedAt: now,
    };
    this.flush(now);
    return this.load().entries[key];
  }

  /**
   * The resume call returned: this session is now owned by this process and
   * the interrupted turn has been closed with synthetic closers.
   */
  confirmStarted(sessionId, turnSeq, now = Date.now()) {
    return this.patch(sessionId, turnSeq, { phase: 'started', updatedAt: now }, now);
  }

  /** The recovery prompt was accepted by the agent's inbox. */
  confirmResumed(sessionId, turnSeq, now = Date.now()) {
    return this.patch(sessionId, turnSeq, { phase: 'resumed', lastError: undefined, updatedAt: now }, now);
  }

  /** The resume failed; record why without pretending the turn is recovered. */
  fail(sessionId, turnSeq, error, now = Date.now()) {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    return this.patch(sessionId, turnSeq, { phase: 'failed', lastError: message.slice(0, 500), updatedAt: now }, now);
  }

  /** The attempt was attempted and deliberately skipped (e.g. writer held elsewhere). */
  skip(sessionId, turnSeq, reason, now = Date.now()) {
    return this.patch(sessionId, turnSeq, { phase: 'skipped', lastError: reason.slice(0, 500), updatedAt: now }, now);
  }

  /** Quarantine a whole session for the configured window. */
  quarantineSession(sessionId, reason, now = Date.now()) {
    this.load().sessions[sessionKey(sessionId)] = {
      phase: 'quarantined',
      until: now + this.config.quarantineMs,
      reason,
      updatedAt: now,
    };
    this.flush(now);
  }

  /** Quarantine one turn (used when the per-turn cap is the binding limit). */
  quarantineTurn(sessionId, turnSeq, reason, now = Date.now()) {
    const entry = this.load().entries[turnKey(sessionId, turnSeq)] ?? {
      sessionId,
      turnSeq,
      phase: 'denied',
      attempts: 0,
      unresolvedIntents: 0,
      firstAt: now,
      updatedAt: now,
    };
    entry.phase = 'quarantined';
    entry.until = now + this.config.quarantineMs;
    entry.lastError = reason.slice(0, 500);
    entry.updatedAt = now;
    this.load().entries[turnKey(sessionId, turnSeq)] = entry;
    this.flush(now);
  }

  /** Drop expired quarantine records and old entries, then persist if anything changed. */
  prune(now = Date.now()) {
    const state = this.load();
    let changed = false;
    for (const [key, session] of Object.entries(state.sessions)) {
      if (session.until <= now) {
        delete state.sessions[key];
        changed = true;
      }
    }
    const keys = Object.keys(state.entries);
    for (const key of keys) {
      const entry = state.entries[key];
      if (now - entry.updatedAt > this.config.retentionMs) {
        delete state.entries[key];
        changed = true;
      }
    }
    const remaining = Object.keys(state.entries);
    if (remaining.length > this.config.maxEntries) {
      remaining
        .sort((a, b) => (state.entries[a].updatedAt ?? 0) - (state.entries[b].updatedAt ?? 0))
        .slice(0, remaining.length - this.config.maxEntries)
        .forEach((key) => {
          delete state.entries[key];
          changed = true;
        });
    }
    if (changed) this.flush(now);
    return changed;
  }

  /**
   * A compact view for the status file and the `/keeper` command.
   * @returns {{entries: number, sessions: number, recent: Array<Record<string, unknown>>}}
   */
  snapshot() {
    const state = this.load();
    const recent = Object.values(state.entries)
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, 20)
      .map((entry) => ({
        sessionId: entry.sessionId,
        turnSeq: entry.turnSeq,
        phase: entry.phase,
        attempts: entry.attempts,
        unresolvedIntents: entry.unresolvedIntents,
        updatedAt: entry.updatedAt,
        lastError: entry.lastError,
      }));
    return { entries: Object.keys(state.entries).length, sessions: Object.keys(state.sessions).length, recent };
  }

  /**
   * @param {string} sessionId
   * @param {number} turnSeq
   * @param {Partial<Entry>} patch
   * @param {number} now
   */
  patch(sessionId, turnSeq, patch, now) {
    const key = turnKey(sessionId, turnSeq);
    const entry = this.load().entries[key];
    if (entry === undefined) return undefined;
    Object.assign(entry, patch, { updatedAt: now });
    this.flush(now);
    return entry;
  }

  /** Persist the document atomically and durably. */
  flush(now = Date.now()) {
    const state = this.load();
    state.updatedAt = now;
    const document = `${JSON.stringify(state, null, 2)}\n`;
    try {
      fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
      const temp = `${this.file}.${process.pid}.${now}.tmp`;
      const fd = fs.openSync(temp, 'w', 0o600);
      try {
        fs.writeSync(fd, document);
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(temp, this.file);
      fsyncDir(this.dir);
    } catch (error) {
      this.note('warn', `ledger write failed: ${/** @type {Error} */ (error).message}`);
    }
  }

  /** @param {'info'|'warn'} level @param {string} message */
  note(level, message) {
    try {
      this.logger?.[level]?.(`session-keeper: ${message}`);
    } catch {
      /* logging must never break recovery */
    }
  }
}

/**
 * fsync a directory so a rename survives power loss. Linux allows it; other
 * platforms may reject it, which is not fatal.
 * @param {string} dir
 */
export function fsyncDir(dir) {
  let fd;
  try {
    fd = fs.openSync(dir, 'r');
    fs.fsyncSync(fd);
  } catch {
    /* best effort */
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        /* ignore */
      }
    }
  }
}

/**
 * Validate one entry read from disk, dropping anything malformed.
 * @param {unknown} value
 * @returns {Record<string, Entry>}
 */
function sanitizeEntries(value) {
  /** @type {Record<string, Entry>} */
  const out = {};
  if (value === null || typeof value !== 'object') return out;
  for (const [key, raw] of Object.entries(value)) {
    if (raw === null || typeof raw !== 'object') continue;
    const entry = /** @type {any} */ (raw);
    if (typeof entry.sessionId !== 'string') continue;
    out[key] = {
      sessionId: entry.sessionId,
      turnSeq: Number(entry.turnSeq) || 0,
      phase: typeof entry.phase === 'string' ? entry.phase : 'failed',
      attempts: Number(entry.attempts) || 0,
      unresolvedIntents: Number(entry.unresolvedIntents) || 0,
      firstAt: Number(entry.firstAt) || 0,
      updatedAt: Number(entry.updatedAt) || 0,
      ...(typeof entry.lastError === 'string' ? { lastError: entry.lastError } : {}),
      ...(Number(entry.until) > 0 ? { until: Number(entry.until) } : {}),
    };
  }
  return out;
}

/**
 * @param {unknown} value
 * @returns {Record<string, SessionEntry>}
 */
function sanitizeSessions(value) {
  /** @type {Record<string, SessionEntry>} */
  const out = {};
  if (value === null || typeof value !== 'object') return out;
  for (const [key, raw] of Object.entries(value)) {
    if (raw === null || typeof raw !== 'object') continue;
    const entry = /** @type {any} */ (raw);
    out[key] = {
      phase: 'quarantined',
      until: Number(entry.until) || 0,
      updatedAt: Number(entry.updatedAt) || 0,
      ...(typeof entry.reason === 'string' ? { reason: entry.reason } : {}),
    };
  }
  return out;
}

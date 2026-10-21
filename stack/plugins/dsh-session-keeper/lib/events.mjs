/**
 * Pure analysis of a durable Session event log.
 *
 * This module is the single source of truth for the question "was this session
 * killed while a turn was open?". It is deliberately dependency-free: the Host
 * plugin feeds it events read through `ctx.sessionPersistence`, and the
 * `dsh-stack` CLI feeds it events decoded from `session.v*.jsonl.zstd`, so the
 * two can never disagree about what a crash looks like.
 *
 * The crash predicate is the same one the persistence layer documents: a log
 * whose tail contains a `turn/start` with no later `turn/end` was interrupted.
 * Resuming such a session makes agent-loop append synthetic closers
 * (`turn/end {interrupted}`), so the same predicate is idempotent: after a
 * successful resume the turn is closed and will not be resumed twice.
 *
 * @module dsh-session-keeper/events
 */

/** Largest number of text characters lifted from one assistant message. */
export const ASSISTANT_NOTE_CHARS = 1200;

/** Text blocks that carry user-visible prompt text. */
const TEXT = 'text';

/**
 * Concatenate the text blocks of one message, in order.
 * @param {unknown} message - a message-like record (`{ content: Block[] }`).
 * @returns {string} the text, whitespace-normalized at the edges.
 */
export function textOf(message) {
  const content = /** @type {{content?: unknown}} */ (message)?.content;
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const block of content) {
    if (
      block !== null &&
      typeof block === 'object' &&
      /** @type {{type?: unknown}} */ (block).type === TEXT &&
      typeof (/** @type {{text?: unknown}} */ (block).text) === 'string'
    ) {
      parts.push(/** @type {{text: string}} */ (block).text);
    }
  }
  return parts.join('\n').trim();
}

/**
 * Shorten text to a bound, marking the elision.
 * @param {string} text
 * @param {number} max
 * @returns {string}
 */
export function clip(text, max) {
  if (typeof text !== 'string') return '';
  if (text.length <= max) return text;
  return text.slice(0, Math.max(0, max - 1)) + '…';
}

/**
 * @typedef {object} LogAnalysis
 * @property {boolean} open - the final turn has no `turn/end` after it.
 * @property {number} [turn] - the open (or last) turn number.
 * @property {number} [turnStartSeq] - seq of the final `turn/start`.
 * @property {number} [turnEndSeq] - seq of the last `turn/end`.
 * @property {boolean} stepOpen - a step is open (crash between step/start and step/end).
 * @property {number} [step] - the open (or last) step number.
 * @property {string[]} pendingUserTexts - user prompts that opened the final turn
 *   (from `user/message` and, when the prompt was queued through the inbox,
 *   from `agent/inbox/spliced`); for an open turn this also includes steering
 *   messages recorded after `turn/start`.
 * @property {string} lastAssistantText - text of the last committed assistant message.
 * @property {string} [lastToolName] - name of the last tool call seen.
 * @property {string} [lastToolResultError] - error identity of the last failed tool result.
 * @property {number} [lastSeq] - highest seq in the supplied slice.
 * @property {number} [lastEventTime] - wall-clock time (epoch ms) of the newest event,
 *   used to tell "interrupted while in flight" from "abandoned days ago".
 * @property {number} eventCount - number of events analysed.
 * @property {boolean} sawTurnBoundary - the slice contained at least one turn/start.
 * @property {boolean} complete - the analysis has everything needed to describe the tail
 *   (a turn boundary was seen and either the opening prompts were seen or the slice
 *   began at seq 0, signalled by {@link LogAnalysis.atLogStart}).
 * @property {boolean} atLogStart - the slice started at offset 0 of the log.
 */

/**
 * Analyse a contiguous slice of a session log.
 *
 * The slice may be the whole log or a tail window; pass `atLogStart` when the
 * slice begins at seq 0 so the analysis can report `complete` even when the
 * opening prompt lies outside the window.
 *
 * @param {readonly any[]} events - events in ascending `seq` order.
 * @param {{atLogStart?: boolean}} [options]
 * @returns {LogAnalysis}
 */
export function analyze(events, options = {}) {
  const atLogStart = options.atLogStart === true;
  /** @type {LogAnalysis} */
  const out = {
    open: false,
    stepOpen: false,
    pendingUserTexts: [],
    lastAssistantText: '',
    eventCount: 0,
    turnStartSeq: undefined,
    turnEndSeq: undefined,
    turn: undefined,
    step: undefined,
    lastToolName: undefined,
    lastToolResultError: undefined,
    lastSeq: undefined,
    lastEventTime: undefined,
    sawTurnBoundary: false,
    complete: false,
    atLogStart,
  };

  /** Prompts queued or recorded since the previous turn closed. */
  let queued = [];
  /** Message ids already recorded, so inbox-spliced prompts are not duplicated. */
  const seenIds = new Set();
  /** Prompts recorded after the final turn started (steering or late materialization). */
  let sinceTurnStart = [];
  /** Highest seq seen; the window's last event. */
  let lastSeq = -1;
  /** Newest event timestamp, for the recency gate. */
  let lastTime;

  /**
   * Record one prompt once.
   * @param {any} message
   * @param {boolean} afterTurnStart
   */
  const recordPrompt = (message, afterTurnStart) => {
    const text = textOf(message);
    if (text.length === 0) return;
    const id = message?.id;
    if (typeof id === 'string' && id.length > 0) {
      if (seenIds.has(id)) return;
      seenIds.add(id);
    }
    if (afterTurnStart) sinceTurnStart.push(text);
    else queued.push(text);
  };

  for (const event of events) {
    if (event === null || typeof event !== 'object') continue;
    const type = /** @type {{type?: unknown}} */ (event).type;
    const seq = /** @type {{seq?: unknown}} */ (event).seq;
    const data = /** @type {{data?: any}} */ (event).data ?? {};
    if (typeof seq === 'number' && seq > lastSeq) lastSeq = seq;
    const time = /** @type {{time?: unknown}} */ (event).time;
    if (typeof time === 'number' && Number.isFinite(time)) lastTime = Math.max(lastTime ?? time, time);
    out.eventCount += 1;

    switch (type) {
      case 'turn/start':
        out.sawTurnBoundary = true;
        out.open = true;
        out.turn = data.turn;
        out.turnStartSeq = seq;
        out.pendingUserTexts = queued;
        queued = [];
        sinceTurnStart = [];
        break;
      case 'turn/end':
        out.sawTurnBoundary = true;
        out.open = false;
        if (data.turn !== undefined) out.turn = data.turn;
        out.turnEndSeq = seq;
        out.turnStartSeq = undefined;
        out.pendingUserTexts = [];
        queued = [];
        sinceTurnStart = [];
        out.stepOpen = false;
        break;
      case 'step/start':
        out.stepOpen = true;
        out.step = data.step;
        break;
      case 'step/end':
        out.stepOpen = false;
        out.step = data.step;
        break;
      case 'user/message': {
        // Materialized form of a human prompt. It can land either before
        // `turn/start` (queued) or after it (steering, or a prompt that was
        // injected through the inbox just before the turn began).
        recordPrompt(data, out.open === true);
        break;
      }
      case 'agent/inbox/spliced': {
        // The pre-materialization form of the same prompt: this is often the
        // only place the request text appears before a very early crash.
        const inserted = Array.isArray(data.inserted) ? data.inserted : [];
        for (const message of inserted) {
          if (message !== null && typeof message === 'object' && message.role === 'user') {
            recordPrompt(message, false);
          }
        }
        break;
      }
      case 'assistant/message': {
        const text = textOf(data.message);
        if (text.length > 0) out.lastAssistantText = clip(text, ASSISTANT_NOTE_CHARS);
        break;
      }
      case 'tool/call':
        if (typeof data.name === 'string') out.lastToolName = data.name;
        break;
      case 'tool/result':
        if (data.error && typeof data.error.name === 'string') out.lastToolResultError = data.error.name;
        break;
      default:
        break;
    }
  }

  // A turn whose prompt is only visible after `turn/start` still has its
  // request recoverable; prefer the pre-turn text, otherwise take the steering
  // text recorded inside the open turn.
  if (out.open === true && out.pendingUserTexts.length === 0 && sinceTurnStart.length > 0) {
    out.pendingUserTexts = sinceTurnStart;
  }

  out.lastSeq = lastSeq >= 0 ? lastSeq : undefined;
  out.lastEventTime = lastTime;
  out.complete = out.sawTurnBoundary && (atLogStart || out.pendingUserTexts.length > 0 || !out.open);
  return out;
}

/**
 * Whether a header identifies a session that must be left alone.
 *
 * Subagent children are excluded by default: their parent owns them, and a
 * standalone resume would fork the work without the parent's context. Forked
 * (seeded) sessions are ordinary sessions and stay eligible.
 *
 * @param {{origin?: string, delegationDepth?: number, parentSession?: string, cwd?: string}} header
 * @param {{includeSubagents?: boolean, cwds?: readonly string[]}} config
 * @returns {{eligible: boolean, reason?: string}}
 */
export function isEligibleHeader(header, config = {}) {
  if (header === null || typeof header !== 'object') return { eligible: false, reason: 'no-header' };
  const isSubagent = header.origin === 'subagent' || (typeof header.delegationDepth === 'number' && header.delegationDepth > 0);
  if (isSubagent && config.includeSubagents !== true) {
    return { eligible: false, reason: 'subagent-session' };
  }
  const allow = Array.isArray(config.cwds) ? config.cwds : [];
  if (allow.length > 0 && !allow.includes(/** @type {string} */ (header.cwd))) {
    return { eligible: false, reason: 'cwd-not-allowlisted' };
  }
  return { eligible: true };
}

/**
 * Render the model-facing recovery prompt.
 *
 * It is deliberately explicit about provenance and about verification: an
 * interrupted turn can leave a tool call half applied, so the first duty of the
 * resumed turn is to re-establish ground truth instead of replaying work.
 *
 * @param {LogAnalysis} analysis
 * @param {{sessionId: string, cwd?: string, attempt: number, maxAttempts: number, interruptedAt?: number, host?: string}} meta
 * @returns {string}
 */
export function buildRecoveryPrompt(analysis, meta) {
  const request = analysis.pendingUserTexts.length > 0
    ? analysis.pendingUserTexts.join('\n\n---\n\n')
    : '(the opening prompt for this turn is not present in the recovered log window)';
  const when = meta.interruptedAt === undefined ? 'an unknown time' : new Date(meta.interruptedAt).toISOString();
  const host = meta.host === undefined ? 'the host' : meta.host;
  const where = typeof meta.cwd === 'string' && meta.cwd.length > 0 ? meta.cwd : '(unknown workspace)';

  const lines = [
    '[dsh-session-keeper] AUTOMATIC CRASH RECOVERY — this turn was started by the supervisor, not by the user.',
    '',
    `The host (${host}) restarted at ${when} while turn ${analysis.turn ?? '?'} of this session was still running.`,
    `The session log is intact; the interrupted turn has been closed as \`interrupted\` (seq ${analysis.turnStartSeq ?? '?'}, workspace ${where}).`,
    `Recovery attempt ${meta.attempt} of at most ${meta.maxAttempts} for this turn.`,
    '',
    'The user request that was in flight when the crash happened was:',
    '"""',
    clip(request, 4000),
    '"""',
    '',
    analysis.lastAssistantText.length > 0
      ? `The last committed assistant message before the crash began: "${clip(analysis.lastAssistantText, ASSISTANT_NOTE_CHARS)}"`
      : 'No assistant message was committed before the crash.',
    analysis.lastToolName === undefined
      ? 'No tool call is known to have been in flight.'
      : `The last tool call observed was \`${analysis.lastToolName}\`${analysis.lastToolResultError === undefined ? '' : ` (last recorded failure: ${analysis.lastToolResultError})`}. A tool call may have been interrupted before its result was recorded.`,
    '',
    'Resume the original request now, following these rules:',
    '1. Re-establish ground truth first: inspect the files, git state, running services, or outputs the work touches before changing anything.',
    '2. Do not repeat steps whose effects are already present. Never re-run a destructive command just because the crash may have interrupted it — verify its effect first.',
    '3. Treat any command whose result was not recorded as "outcome unknown": check before retrying.',
    '4. Continue from the first genuinely unfinished step, then finish the whole original request.',
    '5. End with a short status: what was recovered, what you verified, and anything still outstanding.',
  ];
  return lines.join('\n');
}

/**
 * One-line human summary of an analysis, for logs and the CLI.
 * @param {LogAnalysis} analysis
 * @returns {string}
 */
export function describe(analysis) {
  if (!analysis.open) {
    const turn = analysis.turn === undefined ? '—' : String(analysis.turn);
    return `balanced (last turn ${turn} closed at seq ${analysis.turnEndSeq ?? '?'})`;
  }
  const step = analysis.stepOpen ? `, step ${analysis.step ?? '?'} open` : '';
  return `interrupted in turn ${analysis.turn ?? '?'} (start seq ${analysis.turnStartSeq ?? '?'}${step})`;
}

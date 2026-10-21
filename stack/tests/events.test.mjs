/**
 * Unit tests for the crash predicate and the recovery prompt.
 *
 * These are the load-bearing rules: a false negative loses a session's work, a
 * false positive resumes a session that was never interrupted (and burns the
 * ledger's attempt budget). Both directions are covered.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  analyze,
  buildRecoveryPrompt,
  clip,
  describe,
  isEligibleHeader,
  textOf,
} from '../plugins/dsh-session-keeper/lib/events.mjs';

/** Build an event with a monotonically assigned seq. */
function log(...events) {
  return events.map((event, seq) => ({ seq, ...event }));
}

const userMessage = (text) => ({ type: 'user/message', data: { role: 'user', content: [{ type: 'text', text }] } });
const assistantMessage = (text) => ({ type: 'assistant/message', data: { turn: 1, step: 1, message: { content: [{ type: 'text', text }] } } });
const turnStart = (turn) => ({ type: 'turn/start', data: { turn } });
const turnEnd = (turn, kind = 'completed') => ({ type: 'turn/end', data: { turn, reason: { kind } } });
const stepStart = (turn, step) => ({ type: 'step/start', data: { turn, step } });
const stepEnd = (turn, step) => ({ type: 'step/end', data: { turn, step } });
const toolCall = (name) => ({ type: 'tool/call', data: { name } });
const toolResult = (error) => ({ type: 'tool/result', data: error === undefined ? {} : { error } });

test('a closed log is not interrupted', () => {
  const events = log(userMessage('hello'), turnStart(1), stepStart(1, 1), assistantMessage('hi'), stepEnd(1, 1), turnEnd(1));
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.open, false);
  assert.equal(analysis.turn, 1);
  assert.equal(analysis.turnEndSeq, 5);
  assert.equal(analysis.complete, true);
});

test('the newest event timestamp is exposed for the recency gate', () => {
  const events = log(
    { time: 1_000, type: 'turn/start', data: { turn: 1 } },
    { time: 1_500, type: 'step/start', data: { turn: 1, step: 1 } },
    { time: 1_750, type: 'user/message', data: { role: 'user', content: [{ type: 'text', text: 'go' }] } },
  );
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.lastEventTime, 1_750);
  assert.equal(analysis.open, true);
});

test('events without timestamps leave the recency gate unknown instead of zero', () => {
  const events = log(userMessage('hello'), turnStart(1), stepStart(1, 1));
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.lastEventTime, undefined);
});

test('an open final turn is detected with its opening request', () => {
  const events = log(
    userMessage('first'),
    turnStart(1),
    assistantMessage('done'),
    turnEnd(1),
    userMessage('the interrupted request'),
    turnStart(2),
    stepStart(2, 1),
  );
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.open, true);
  assert.equal(analysis.turn, 2);
  assert.equal(analysis.turnStartSeq, 5);
  assert.equal(analysis.stepOpen, true);
  assert.deepEqual(analysis.pendingUserTexts, ['the interrupted request']);
  assert.equal(analysis.complete, true);
  assert.match(describe(analysis), /interrupted in turn 2/);
});

test('a tail window that starts inside the turn is inconclusive, not "closed"', () => {
  // Window begins after turn/start: no boundary is visible, so the caller must
  // expand rather than conclude the turn is closed.
  const events = log(
    { type: 'step/start', data: { turn: 2, step: 3 } },
    assistantMessage('partial'),
    toolCall('bash'),
  );
  const analysis = analyze(events, { atLogStart: false });
  assert.equal(analysis.sawTurnBoundary, false);
  assert.equal(analysis.complete, false);
  assert.equal(analysis.open, false);
});

test('a window with a boundary but no opening prompt is incomplete unless it starts at seq 0', () => {
  const events = log(turnStart(2), assistantMessage('partial'));
  const tail = analyze(events, { atLogStart: false });
  assert.equal(tail.open, true);
  assert.equal(tail.complete, false);

  const whole = analyze(events, { atLogStart: true });
  assert.equal(whole.complete, true);
});

test('multiple steering user messages are all preserved', () => {
  const events = log(userMessage('one'), userMessage('two'), turnStart(1));
  const analysis = analyze(events, { atLogStart: true });
  assert.deepEqual(analysis.pendingUserTexts, ['one', 'two']);
});

test('textOf reads only text blocks', () => {
  assert.equal(textOf({ content: [{ type: 'text', text: ' a ' }, { type: 'image', data: 'x' }, { type: 'text', text: 'b' }] }), 'a \nb');
  assert.equal(textOf({}), '');
  assert.equal(textOf(null), '');
});

test('the last failed tool is surfaced for the recovery prompt', () => {
  const events = log(userMessage('go'), turnStart(1), toolCall('write_file'), toolResult({ name: 'ToolOutcomeUnknownError' }));
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.lastToolName, 'write_file');
  assert.equal(analysis.lastToolResultError, 'ToolOutcomeUnknownError');
  const prompt = buildRecoveryPrompt(analysis, { sessionId: 's1', attempt: 1, maxAttempts: 1, host: 'box' });
  assert.match(prompt, /write_file/);
  assert.match(prompt, /ToolOutcomeUnknownError/);
});

test('subagent sessions are excluded, forks are not', () => {
  assert.equal(isEligibleHeader({ origin: 'subagent' }, {}).eligible, false);
  assert.equal(isEligibleHeader({ delegationDepth: 2 }, {}).eligible, false);
  assert.equal(isEligibleHeader({ parentSession: 'p', origin: undefined }, {}).eligible, true);
  assert.equal(isEligibleHeader({ origin: 'subagent' }, { includeSubagents: true }).eligible, true);
});

test('the cwd allowlist is honoured', () => {
  assert.deepEqual(isEligibleHeader({ cwd: '/a' }, { cwds: ['/a'] }), { eligible: true });
  assert.deepEqual(isEligibleHeader({ cwd: '/b' }, { cwds: ['/a'] }), { eligible: false, reason: 'cwd-not-allowlisted' });
  assert.deepEqual(isEligibleHeader({ cwd: '/b' }, { cwds: [] }), { eligible: true });
});

test('the recovery prompt names provenance, the request, and the safety rules', () => {
  const events = log(userMessage('refactor the parser and run the tests'), turnStart(3), assistantMessage('editing parser.mjs'));
  const analysis = analyze(events, { atLogStart: true });
  const prompt = buildRecoveryPrompt(analysis, {
    sessionId: 'session-1',
    cwd: '/work',
    attempt: 1,
    maxAttempts: 1,
    interruptedAt: Date.UTC(2026, 9, 20, 12, 0, 0),
    host: 'host-a',
  });
  assert.match(prompt, /AUTOMATIC CRASH RECOVERY/);
  assert.match(prompt, /host-a/);
  assert.match(prompt, /2026-10-20T12:00:00/);
  assert.match(prompt, /refactor the parser and run the tests/);
  assert.match(prompt, /editing parser\.mjs/);
  assert.match(prompt, /Re-establish ground truth/);
  assert.match(prompt, /Never re-run a destructive command/);
  assert.match(prompt, /turn 3/);
});

test('a very long request is clipped, never dropped', () => {
  const long = 'x'.repeat(50_000);
  const events = log(userMessage(long), turnStart(1));
  const analysis = analyze(events, { atLogStart: true });
  const prompt = buildRecoveryPrompt(analysis, { sessionId: 's', attempt: 1, maxAttempts: 2 });
  assert.ok(prompt.length < 12_000, `prompt should stay bounded, got ${prompt.length}`);
  assert.match(prompt, /xxxx/);
  assert.equal(clip('abcdef', 4), 'abc…');
});

test('a prompt queued through the inbox before turn/start is recovered', () => {
  // The shape a real log has: the prompt is spliced into the inbox and only
  // materializes as user/message later. A crash before that point must still
  // recover the request text.
  const id = 'message-1';
  const events = log(
    { type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, inserted: [{ role: 'user', id, content: [{ type: 'text', text: 'deploy the new build' }] }] } },
    turnStart(1),
    stepStart(1, 1),
  );
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.open, true);
  assert.deepEqual(analysis.pendingUserTexts, ['deploy the new build']);
});

test('a prompt materialized after turn/start is recovered without duplication', () => {
  const id = 'message-2';
  const message = { role: 'user', id, content: [{ type: 'text', text: 'rename the field everywhere' }] };
  const events = log(
    { type: 'agent/inbox/spliced', data: { target: 'next-turn', start: 0, inserted: [message] } },
    turnStart(1),
    stepStart(1, 1),
    { type: 'user/message', data: message },
  );
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.open, true);
  assert.deepEqual(analysis.pendingUserTexts, ['rename the field everywhere'], 'the same prompt must appear once');
});

test('steering messages recorded inside the open turn are recovered when the opener is missing', () => {
  const events = log(
    turnStart(2),
    stepStart(2, 1),
    { type: 'user/message', data: { role: 'user', id: 'steer-1', content: [{ type: 'text', text: 'actually use the v2 endpoint' }] } },
  );
  const analysis = analyze(events, { atLogStart: true });
  assert.equal(analysis.open, true);
  assert.deepEqual(analysis.pendingUserTexts, ['actually use the v2 endpoint']);
});

/**
 * Tests for the recovery ledger: the idempotency and crash-loop guard.
 *
 * The scenarios mirror the failure modes that matter:
 * - a crash before the resume confirmed (intent with no `started`);
 * - a turn that was already resumed (must never run twice);
 * - a poison-pill session that keeps crashing the host (quarantine).
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { RecoveryLedger, planAttempt, LEDGER_DEFAULTS } from '../plugins/dsh-session-keeper/lib/ledger.mjs';

/** A fresh temp directory per test. */
function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'keeper-ledger-'));
}

test('a fresh turn is allowed and spends one attempt', () => {
  const plan = planAttempt({ sessionAttempts: 0, now: 1_000, config: LEDGER_DEFAULTS });
  assert.equal(plan.allow, true);
  assert.equal(plan.spend, 'attempt');
  assert.equal(plan.attempts, 1);
});

test('a resumed turn is never resumed again', () => {
  const ledger = new RecoveryLedger({ file: path.join(tempDir(), 'ledger.json') });
  const now = Date.now();
  const plan = ledger.decide('s1', 42, now);
  ledger.begin('s1', 42, plan, now);
  ledger.confirmStarted('s1', 42, now);
  ledger.confirmResumed('s1', 42, now);

  const again = ledger.decide('s1', 42, now + 1_000);
  assert.equal(again.allow, false);
  assert.equal(again.reason, 'already-resumed');
  assert.equal(again.spend, 'none');
});

test('a crash between intent and started may retry, but only a bounded number of times', () => {
  const dir = tempDir();
  const ledger = new RecoveryLedger({ file: path.join(dir, 'ledger.json'), config: { maxUnresolvedIntents: 1 } });
  let now = Date.now();
  // Simulate a process that dies every time right after writing the intent.
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const plan = ledger.decide('s1', 7, now);
    assert.equal(plan.allow, true, `attempt ${attempt} should be allowed`);
    ledger.begin('s1', 7, plan, now);
    now += 5;
  }
  const denied = ledger.decide('s1', 7, now);
  assert.equal(denied.allow, false);
  assert.equal(denied.reason, 'unresolved-intent-limit');
});

test('the per-turn cap is enforced after a confirmed attempt', () => {
  const dir = tempDir();
  const ledger = new RecoveryLedger({ file: path.join(dir, 'ledger.json'), config: { maxAttemptsPerTurn: 1 } });
  const now = Date.now();
  const plan = ledger.decide('s1', 9, now);
  ledger.begin('s1', 9, plan, now);
  ledger.confirmStarted('s1', 9, now);
  ledger.fail('s1', 9, new Error('model route died'), now + 1);

  const again = ledger.decide('s1', 9, now + 2);
  assert.equal(again.allow, false);
  assert.equal(again.reason, 'turn-attempt-limit');
});

test('a session that keeps crashing is quarantined, and the quarantine expires', () => {
  const dir = tempDir();
  const ledger = new RecoveryLedger({
    file: path.join(dir, 'ledger.json'),
    config: { maxAttemptsPerSession: 2, quarantineMs: 60_000, sessionWindowMs: 3_600_000 },
  });
  let now = Date.now();
  for (let turn = 1; turn <= 2; turn += 1) {
    const plan = ledger.decide('poison', turn, now);
    assert.equal(plan.allow, true);
    ledger.begin('poison', turn, plan, now);
    ledger.confirmStarted('poison', turn, now);
    ledger.confirmResumed('poison', turn, now);
    now += 1_000;
  }
  // Third crash in the same window: the session budget is gone.
  const denied = ledger.decide('poison', 3, now);
  assert.equal(denied.allow, false);
  assert.equal(denied.reason, 'session-attempt-limit');
  ledger.quarantineSession('poison', denied.reason, now);

  const stillDenied = ledger.decide('poison', 4, now + 1_000);
  assert.equal(stillDenied.allow, false);
  assert.match(stillDenied.reason, /session-quarantined/);

  // After the quarantine expires (and the window has passed) recovery resumes.
  assert.equal(ledger.decide('poison', 5, now + 3_600_002).allow, true);
});

test('a corrupt ledger is quarantined and recovery continues from empty', () => {
  const dir = tempDir();
  const file = path.join(dir, 'ledger.json');
  fs.writeFileSync(file, '{ this is not json');
  const warnings = [];
  const ledger = new RecoveryLedger({ file, logger: { warn: (message) => warnings.push(message) } });
  const plan = ledger.decide('s1', 1, Date.now());
  assert.equal(plan.allow, true);
  assert.ok(fs.readdirSync(dir).some((name) => name.includes('.corrupt-')), 'corrupt ledger should be preserved');
  assert.ok(warnings.some((message) => message.includes('corrupt')));
});

test('the document survives a reload and keeps its decisions', () => {
  const dir = tempDir();
  const file = path.join(dir, 'ledger.json');
  const first = new RecoveryLedger({ file });
  const now = Date.now();
  const plan = first.decide('s1', 3, now);
  first.begin('s1', 3, plan, now);
  first.confirmStarted('s1', 3, now);
  first.confirmResumed('s1', 3, now);

  const second = new RecoveryLedger({ file });
  const decision = second.decide('s1', 3, Date.now());
  assert.equal(decision.allow, false);
  assert.equal(decision.reason, 'already-resumed');
  assert.equal(second.snapshot().recent[0].phase, 'resumed');
});

test('pruning drops old entries and expired quarantines', () => {
  const dir = tempDir();
  const ledger = new RecoveryLedger({ file: path.join(dir, 'ledger.json'), config: { retentionMs: 1_000, quarantineMs: 1_000 } });
  const now = Date.now();
  const plan = ledger.decide('s1', 1, now);
  ledger.begin('s1', 1, plan, now);
  ledger.quarantineSession('s1', 'test', now);
  assert.equal(Object.keys(ledger.entries()).length, 1);

  ledger.prune(now + 5_000);
  assert.equal(Object.keys(ledger.entries()).length, 0);
  assert.equal(Object.keys(ledger.sessions()).length, 0);
});

test('a malformed entry on disk is dropped instead of poisoning the run', () => {
  const dir = tempDir();
  const file = path.join(dir, 'ledger.json');
  fs.writeFileSync(
    file,
    JSON.stringify({
      version: 1,
      updatedAt: 1,
      entries: {
        good: { sessionId: 's1', turnSeq: 1, phase: 'resumed', attempts: 1, unresolvedIntents: 0, firstAt: 1, updatedAt: 1 },
        bad: { turnSeq: 'nope' },
      },
      sessions: { 'session:old': { phase: 'quarantined', until: 0, updatedAt: 0 } },
    }),
  );
  const ledger = new RecoveryLedger({ file });
  const entries = ledger.entries();
  assert.equal(Object.keys(entries).length, 1);
  assert.equal(entries.good.phase, 'resumed');
});

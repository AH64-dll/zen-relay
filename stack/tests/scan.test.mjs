/**
 * Tests for the offline session scanner.
 *
 * The scanner is the supervisor's independent second opinion, so it is tested
 * against synthetic v4 stores (exact control over every byte) and against the
 * real store on this machine when one exists (read-only smoke test).
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { randomBytes } from 'node:crypto';

import { scanInterrupted, listStoredSessions } from '../lib/sessionscan.mjs';

/**
 * Write one stored session in the on-disk shape the harness uses.
 * @param {string} home
 * @param {string} project
 * @param {string} id
 * @param {any[]} events events without seq
 * @param {{cwd?: string, origin?: string, delegationDepth?: number}} [header]
 */
function writeSession(home, project, id, events, header = {}) {
  const dir = path.join(home, 'sessions', project, id);
  fs.mkdirSync(dir, { recursive: true });
  const lines = [
    JSON.stringify({
      type: 'session',
      version: 4,
      id,
      createdAt: Date.now(),
      cwd: header.cwd ?? '/tmp/work',
      isSeeded: false,
      ...(header.origin === undefined ? {} : { origin: header.origin }),
      ...(header.delegationDepth === undefined ? {} : { delegationDepth: header.delegationDepth }),
    }),
    ...events.map((event, seq) => JSON.stringify({ seq, ...event })),
  ];
  fs.writeFileSync(path.join(dir, 'session.v4.jsonl.zstd'), zlib.zstdCompressSync(Buffer.from(`${lines.join('\n')}\n`)));
  fs.writeFileSync(path.join(dir, 'session.lock'), '');
  return dir;
}

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-stack-scan-'));
}

const user = (text) => ({ type: 'user/message', data: { role: 'user', content: [{ type: 'text', text }] } });
const start = (turn) => ({ type: 'turn/start', data: { turn } });
const end = (turn) => ({ type: 'turn/end', data: { turn, reason: { kind: 'completed' } } });

test('a balanced store is fully understood and nothing is reported', () => {
  const home = tempHome();
  writeSession(home, '--work--', 'session-a', [user('hi'), start(1), end(1)]);
  writeSession(home, '--work--', 'session-b', []);
  const scan = scanInterrupted({ dshHome: home });
  assert.equal(scan.scanned, 2);
  assert.equal(scan.interrupted.length, 0);
  assert.equal(scan.balanced, 2, 'a header-only session is decodable and closed');
  assert.equal(scan.errors.length, 0);
});

test('an open turn is reported with its request', () => {
  const home = tempHome();
  writeSession(home, '--work--', 'session-crashed', [user('finish the migration'), start(4)]);
  const scan = scanInterrupted({ dshHome: home });
  assert.equal(scan.interrupted.length, 1);
  const entry = scan.interrupted[0];
  assert.equal(entry.sessionId, 'session-crashed');
  assert.equal(entry.analysis.turn, 4);
  assert.equal(entry.analysis.turnStartSeq, 1);
  assert.deepEqual(entry.analysis.pendingUserTexts, ['finish the migration']);
  assert.equal(entry.header.cwd, '/tmp/work');
});

test('an unreadable artifact is reported instead of aborting the scan', () => {
  const home = tempHome();
  writeSession(home, '--work--', 'session-good', [user('x'), start(1)]);
  const dir = path.join(home, 'sessions', '--work--', 'session-corrupt');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'session.v4.jsonl.zstd'), Buffer.from('not zstd at all'));
  const scan = scanInterrupted({ dshHome: home });
  assert.equal(scan.scanned, 2);
  assert.equal(scan.interrupted.length, 1);
  assert.equal(scan.errors.length, 1);
  assert.match(scan.errors[0].file, /session-corrupt/);
});

test('an already-resumed log (synthetic closer appended) is balanced', () => {
  const home = tempHome();
  // What the persistence layer writes after a successful resume: the open turn
  // is closed with the `interrupted` reason, plus step closers.
  writeSession(home, '--work--', 'session-recovered', [
    user('do the thing'),
    start(2),
    { type: 'step/start', data: { turn: 2, step: 1 } },
    { type: 'step/end', data: { turn: 2, step: 1 } },
    { type: 'turn/end', data: { turn: 2, reason: { kind: 'interrupted' } } },
  ]);
  const scan = scanInterrupted({ dshHome: home });
  assert.equal(scan.interrupted.length, 0);
  assert.equal(scan.balanced, 1);
});

test('log size is bounded and oversized artifacts are skipped with a reason', () => {
  const home = tempHome();
  // Incompressible content, so the on-disk artifact really exceeds the bound.
  writeSession(home, '--work--', 'session-huge', [user(randomBytes(8_192).toString('hex')), start(1)]);
  const scan = scanInterrupted({ dshHome: home, maxBytes: 1_000 });
  assert.equal(scan.interrupted.length, 0);
  assert.equal(scan.errors.length, 1);
  assert.match(scan.errors[0].error, /larger than/);
});

test('subagent metadata is exposed so the caller can filter it', () => {
  const home = tempHome();
  writeSession(home, '--work--', 'session-child', [user('child work'), start(1)], { origin: 'subagent', delegationDepth: 1 });
  const scan = scanInterrupted({ dshHome: home });
  assert.equal(scan.interrupted.length, 1);
  assert.equal(scan.interrupted[0].header.origin, 'subagent');
  assert.equal(scan.interrupted[0].header.delegationDepth, 1);
});

test('resumable excludes subagent children while interrupted still reports them', () => {
  const home = tempHome();
  writeSession(home, '--work--', 'session-parent', [user('parent work'), start(1)]);
  writeSession(home, '--work--', 'session-child', [user('child work'), start(1)], {
    origin: 'subagent',
    delegationDepth: 1,
  });

  const scan = scanInterrupted({ dshHome: home });
  assert.equal(scan.interrupted.length, 2, 'the raw list keeps everything for reporting');
  assert.deepEqual(scan.resumable.map((entry) => entry.sessionId), ['session-parent']);
  assert.equal(scan.children, 1);
  assert.equal(scan.interrupted.find((entry) => entry.sessionId === 'session-child').eligible, false);
});

test('the newest copy wins when one session id exists twice', () => {
  const home = tempHome();
  writeSession(home, '--old--', 'session-dup', [user('old'), start(1), end(1)]);
  const older = path.join(home, 'sessions', '--old--', 'session-dup', 'session.v4.jsonl.zstd');
  fs.utimesSync(older, new Date(1_000), new Date(1_000));
  writeSession(home, '--new--', 'session-dup', [user('new'), start(2)]);
  const scan = scanInterrupted({ dshHome: home });
  assert.equal(scan.scanned, 1);
  assert.equal(scan.interrupted.length, 1);
  assert.equal(scan.interrupted[0].project, '--new--');
});

test('listStoredSessions returns metadata without requiring a parse of every event', () => {
  const home = tempHome();
  writeSession(home, '--work--', 'session-a', [user('hi'), start(1), end(1)], { cwd: '/srv/app' });
  const list = listStoredSessions({ dshHome: home });
  assert.equal(list.length, 1);
  assert.equal(list[0].sessionId, 'session-a');
  assert.equal(list[0].cwd, '/srv/app');
});

test('a missing session store is an empty scan, not an error', () => {
  const scan = scanInterrupted({ dshHome: path.join(os.tmpdir(), 'definitely-not-a-dsh-home') });
  assert.equal(scan.scanned, 0);
  assert.equal(scan.interrupted.length, 0);
});

test('the real store on this machine scans without throwing (read-only)', () => {
  const home = path.join(os.homedir(), '.dsh');
  if (!fs.existsSync(path.join(home, 'sessions'))) return;
  const scan = scanInterrupted({ dshHome: home, limit: 500 });
  assert.ok(scan.scanned > 0, 'expected at least one stored session');
  for (const entry of scan.interrupted) {
    assert.equal(entry.analysis.open, true);
    assert.ok(entry.sizeBytes > 0);
    assert.ok(typeof entry.header.id === 'string' && entry.header.id.length > 0);
  }
  assert.deepEqual(
    scan.interrupted.map((entry) => entry.sessionId).length,
    new Set(scan.interrupted.map((entry) => entry.sessionId)).size,
    'a session id must not be reported twice',
  );
});

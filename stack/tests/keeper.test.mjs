/**
 * Integration tests for the recovery engine against the **real** persistence
 * backend shipped with the Harness.
 *
 * Fake services keep the test hermetic, but storage is the production JSONL
 * backend, so these tests exercise the exact behaviours that matter in an
 * incident:
 *
 * - detection of a genuine crashed log (written through `append`/`flush`, not a
 *   hand-crafted file);
 * - repair parity: the closers the engine triggers are the Harness's own
 *   `interruptedTurnClosers`, and afterwards the log reads balanced — which is
 *   what makes recovery idempotent across any crash point;
 * - the single-writer lease (`SessionAlreadyOwnedError`) is honoured instead of
 *   being raced;
 * - a live agent in this Host is never resumed;
 * - the ledger, status document and dry-run mode behave as documented.
 *
 * The tests skip themselves when the Harness packages are not installed, so the
 * suite still runs on a machine that only has this repository.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const HARNESS = locateHarness();

/** @type {any} */
let cordis;
/** @type {any} */
let JsonlBackend;
/** @type {any} */
let interruptedTurnClosers;

const skip = HARNESS === undefined ? 'Harness packages not installed on this machine' : false;

/** @param {string} dir */
function module(file) {
  return path.join(/** @type {string} */ (HARNESS), file);
}

if (HARNESS !== undefined) {
  cordis = await import(module('cordis/lib/index.js'));
  JsonlBackend = (await import(module('dsh-session-persistence-jsonl/lib/index.js'))).default;
  interruptedTurnClosers = (await import(module('dsh-session/lib/index.js'))).interruptedTurnClosers;
}

/** The engine under test is imported lazily so the module graph stays simple. */
const { SessionKeeper, normalizeConfig } = await import('../plugins/dsh-session-keeper/lib/keeper.mjs');

/**
 * @typedef {object} Harness
 * @property {string} root
 * @property {string} workspace
 * @property {any} persistence
 * @property {any} agents
 * @property {any} keeper
 * @property {Record<string, any>} calls
 * @property {any} ctx
 */

/**
 * Build one isolated store + keeper with fake host services.
 * @param {{session?: Record<string, unknown>, config?: Record<string, unknown>}} [options]
 * @returns {Promise<Harness>}
 */
async function makeHarness(options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'keeper-it-'));
  const workspace = path.join(root, 'workspace');
  const stateDir = path.join(root, 'state');
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(stateDir, { recursive: true });

  const persistence = new JsonlBackend(new cordis.Context(), { root: path.join(root, 'sessions') });
  const calls = { resume: [], followups: [], mounted: [], disposed: 0 };

  /** @type {any} */
  const agents = {
    /** @param {string} id */
    get: (id) => (options.session?.liveId === id ? { id } : undefined),
    resume: async (/** @type {any} */ resumeOptions) => {
      calls.resume.push(resumeOptions);
      const handle = await persistence.open(resumeOptions.resumeSessionId, 'write');
      // The real factory runs setup against an unpublished agent context first;
      // the fake does the same so preset composition is observable.
      if (typeof resumeOptions.setup === 'function') await resumeOptions.setup({}, { id: resumeOptions.resumeSessionId });
      try {
        const { events } = await handle.read();
        const closers = interruptedTurnClosers(events);
        if (closers.length > 0) await handle.append(closers);
        await handle.flush();
      } finally {
        await handle.close();
      }
      const followups = [];
      calls.followups = followups;
      const agent = {
        id: resumeOptions.resumeSessionId,
        followup: (/** @type {any} */ message) => followups.push(message),
        cancel: () => {},
        whenIdle: async () => {},
        status: 'idle',
      };
      return { agent, dispose: async () => { calls.disposed += 1; } };
    },
  };

  const presets = {
    resolve: async (/** @type {string|undefined} */ id) => ({ id: id ?? 'general' }),
    mount: async (/** @type {any} */ agentCtx, /** @type {string|undefined} */ id) => { calls.mounted.push(id); },
  };
  const model = { currentSelection: () => ({ provider: 'test-provider', model: 'test-model' }) };

  const services = { sessionPersistence: persistence, agents, agentPresets: presets, agentDefaultModel: model };
  const ctx = {
    /** @param {string} name */
    get: (name) => services[name],
    logger: { info: () => {}, warn: () => {}, error: () => {} },
  };

  const config = normalizeConfig({
    settleMs: 0,
    requireProxy: false,
    stateDir,
    ...(options.session?.config ?? {}),
  });
  const keeper = new SessionKeeper(ctx, config);
  return { root, workspace, persistence, agents, keeper, calls, ctx };
}

/**
 * Create a stored session whose final turn is open, exactly as a crash leaves it.
 * @param {Harness} harness
 * @param {{cwd?: string, events?: any[], header?: Record<string, unknown>, ageMs?: number}} [options]
 * @returns {Promise<{id: string, events: any[]}>}
 */
async function createInterrupted(harness, options = {}) {
  const id = `session-${randomUUID()}`;
  // `ageMs` shifts the whole log into the past: the recency gate keys on event
  // timestamps, so this is enough to look like an abandoned turn.
  const now = Date.now() - (options.ageMs ?? 0);
  const cwd = options.cwd ?? harness.workspace;
  const header = { version: 4, id, createdAt: now, cwd, isSeeded: false, ...(options.header ?? {}) };
  const handle = await harness.persistence.create(header);
  const events = options.events ?? [
    event(0, 'turn/start', { turn: 1 }),
    event(1, 'step/start', { turn: 1, step: 1 }),
    // No seq for the user message: the log is what a crash leaves behind.
    event(2, 'user/message', {
      content: [{ type: 'text', text: 'Add retries to the uploader and run the tests.' }],
      source: { kind: 'user' },
      role: 'user',
      id: randomUUID(),
    }, 'append'),
  ];
  await handle.append(events);
  await handle.flush();
  await handle.close();
  return { id, events };

  /** @param {number} seq @param {string} type @param {any} data @param {string} [surfaceOp] */
  function event(seq, type, data, surfaceOp) {
    return {
      type,
      seq,
      time: now + seq,
      data,
      ...(surfaceOp === undefined ? {} : { surfaceOp }),
    };
  }
}

/** Read the ledger/status documents the keeper wrote. */
function readState(harness) {
  const stateDir = path.join(harness.root, 'state');
  const status = JSON.parse(fs.readFileSync(path.join(stateDir, 'status.json'), 'utf8'));
  const ledgerFile = path.join(stateDir, 'ledger.json');
  const ledger = fs.existsSync(ledgerFile) ? JSON.parse(fs.readFileSync(ledgerFile, 'utf8')) : { entries: {} };
  return { status, ledger };
}

test('an interrupted turn is resumed once, repaired, and never resumed again', { skip }, async () => {
  const harness = await makeHarness();
  const { id } = await createInterrupted(harness);

  const summary = await harness.keeper.runOnce({ trigger: 'boot' });
  assert.equal(summary.phase, 'done');
  assert.equal(summary.candidates, 1, 'the crashed session must be detected');
  assert.equal(summary.resumed.length, 1);
  assert.equal(harness.calls.resume.length, 1, 'resume must be called exactly once');
  assert.equal(harness.calls.resume[0].resumeSessionId, id);
  assert.equal(harness.calls.resume[0].agentOptions.provider, 'test-provider');
  assert.deepEqual(harness.calls.mounted, ['general'], 'the session preset is composed before publication');

  const prompt = harness.calls.followups[0];
  assert.equal(prompt.role, 'user');
  assert.match(prompt.content[0].text, /AUTOMATIC CRASH RECOVERY/);
  assert.match(prompt.content[0].text, /Add retries to the uploader and run the tests\./);

  // The fake resume repaired the log exactly as agent-loop does. A second pass
  // must therefore find nothing to resume even with an empty ledger — this is
  // the property that makes recovery idempotent at any crash point.
  fs.rmSync(path.join(harness.root, 'state', 'ledger.json'));
  harness.keeper.ledger = new (await import('../plugins/dsh-session-keeper/lib/ledger.mjs')).RecoveryLedger({
    file: path.join(harness.root, 'state', 'ledger.json'),
  });
  const second = await harness.keeper.runOnce({ trigger: 'boot' });
  assert.equal(second.candidates, 0, 'a repaired log must not look interrupted');
  assert.equal(harness.calls.resume.length, 1);

  const state = readState(harness);
  assert.equal(state.status.summary.phase, 'done');
  assert.equal(state.status.pid, process.pid);
});

test('a repeated crash on the same open turn is bounded by the ledger', { skip }, async () => {
  const harness = await makeHarness();
  const { id } = await createInterrupted(harness);

  // Simulate a resumed agent that died before repairing the log: the log stays
  // open, so only the ledger can stop a second attempt.
  harness.agents.resume = async (/** @type {any} */ options) => {
    harness.calls.resume.push(options);
    return { agent: { id: options.resumeSessionId, followup: () => {}, cancel: () => {}, whenIdle: async () => {} }, dispose: async () => {} };
  };

  await harness.keeper.runOnce({ trigger: 'boot' });
  const second = await harness.keeper.runOnce({ trigger: 'manual' });
  assert.equal(harness.calls.resume.length, 1, 'a confirmed attempt must not be repeated');
  assert.ok(second.skipped.some((entry) => entry.reason === 'already-resumed'), JSON.stringify(second.skipped));
  const ledger = readState(harness).ledger;
  assert.equal(ledger.entries[`${id}#0`].phase, 'resumed');
});

test('dry run decides without resuming and without spending the ledger', { skip }, async () => {
  const harness = await makeHarness({ session: { config: { dryRun: true } } });
  const { id } = await createInterrupted(harness);

  const summary = await harness.keeper.runOnce({ trigger: 'manual' });
  assert.equal(summary.resumed.length, 1);
  assert.equal(summary.resumed[0].status, 'dry-run');
  assert.equal(harness.calls.resume.length, 0);
  assert.deepEqual(readState(harness).ledger.entries, {});

  // A dry run must not consume the real attempt: the following real run works.
  harness.keeper.config.dryRun = false;
  const real = await harness.keeper.runOnce({ trigger: 'manual' });
  assert.equal(real.resumed.length, 1);
  assert.equal(real.resumed[0].status, 'resumed', JSON.stringify(real.skipped));
  assert.equal(harness.calls.resume[0].resumeSessionId, id);
});

test('a session live in this Host is skipped, not resumed', { skip }, async () => {
  const harness = await makeHarness();
  const { id } = await createInterrupted(harness);
  // Pretend this Host already has the agent mounted.
  harness.agents.get = (/** @type {string} */ candidate) => (candidate === id ? { id } : undefined);

  const summary = await harness.keeper.runOnce({ trigger: 'boot' });
  assert.equal(harness.calls.resume.length, 0);
  assert.ok(summary.skipped.some((entry) => entry.sessionId === id && entry.reason === 'live-in-this-host'));
});

test('another process holding the write lease wins; recovery backs off', { skip }, async () => {
  const harness = await makeHarness();
  const { id } = await createInterrupted(harness);
  const owner = await harness.persistence.open(id, 'write');
  try {
    const summary = await harness.keeper.runOnce({ trigger: 'boot' });
    assert.equal(summary.resumed.length, 0);
    assert.ok(
      summary.skipped.some((entry) => entry.reason === 'writer-held-elsewhere'),
      JSON.stringify(summary.skipped),
    );
    const ledger = readState(harness).ledger;
    const entry = Object.values(ledger.entries)[0];
    assert.equal(entry.phase, 'skipped', 'a lost lease must not burn the turn attempt');
  } finally {
    await owner.close();
  }
});

test('subagent sessions and missing workspaces are left alone', { skip }, async () => {
  const harness = await makeHarness();
  const child = await createInterrupted(harness, { header: { delegationDepth: 1, origin: 'subagent' } });
  const missing = await createInterrupted(harness, { cwd: path.join(harness.root, 'does-not-exist') });

  const summary = await harness.keeper.runOnce({ trigger: 'boot' });
  assert.equal(harness.calls.resume.length, 0);
  assert.ok(summary.skipped.some((entry) => entry.sessionId === child.id && entry.reason === 'subagent-session'));
  assert.ok(summary.skipped.some((entry) => entry.sessionId === missing.id && entry.reason === 'workspace-missing'));
});

test('several interrupted sessions are resumed up to the per-boot cap', { skip }, async () => {
  const harness = await makeHarness({ session: { config: { maxResumesPerBoot: 2, maxConcurrent: 2 } } });
  const base = Date.now();
  const first = await createInterrupted(harness, { header: { createdAt: base + 1_000 } });
  const second = await createInterrupted(harness, { header: { createdAt: base + 2_000 } });
  const third = await createInterrupted(harness, { header: { createdAt: base + 3_000 } });

  const summary = await harness.keeper.runOnce({ trigger: 'boot' });
  assert.equal(summary.candidates, 3);
  assert.equal(harness.calls.resume.length, 2, 'the cap must be enforced');
  assert.equal(summary.resumed.length, 2);
  const resumedIds = harness.calls.resume.map((call) => call.resumeSessionId).sort();
  assert.deepEqual(resumedIds, [second.id, third.id].sort(), 'the two newest sessions are resumed first');
  assert.ok(!resumedIds.includes(first.id));
});

// ------------------------------------------------------------------------ helpers

/** Find the Harness installation's node_modules, if present. */
test('a stale open turn is not auto-resumed, but recovery on demand still works', { skip }, async () => {
  const harness = await makeHarness();
  await createInterrupted(harness, { ageMs: 48 * 60 * 60 * 1000 });

  const summary = await harness.keeper.runOnce({ trigger: 'boot' });
  assert.equal(summary.candidates, 1);
  assert.equal(summary.resumed.length, 0, 'a two-day-old open turn is abandoned work, not a crash');
  assert.ok(
    summary.skipped.some((entry) => /session-too-old/.test(String(entry.reason))),
    'the skip must name the recency gate so operators can override it',
  );
  assert.equal(harness.calls.resume.length, 0);

  // The ledger must not have been charged for the age skip.
  const manual = await harness.keeper.runOnce({ trigger: 'manual' });
  assert.equal(manual.resumed.length, 1, 'an explicit /keeper recover bypasses the recency gate');
  assert.equal(harness.calls.resume.length, 1);
});

test('maxAgeMs=0 disables the recency gate', { skip }, async () => {
  const harness = await makeHarness({ session: { config: { maxAgeMs: 0 } } });
  await createInterrupted(harness, { ageMs: 48 * 60 * 60 * 1000 });
  const summary = await harness.keeper.runOnce({ trigger: 'boot' });
  assert.equal(summary.resumed.length, 1);
});

function locateHarness() {
  const candidates = [];
  if (process.env.DSH_STACK_HARNESS_NODE_MODULES) candidates.push(process.env.DSH_STACK_HARNESS_NODE_MODULES);
  candidates.push('/home/ai/.local/node24/node-v24.22.0-linux-x64/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai');
  candidates.push('/usr/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai');
  candidates.push('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai');
  for (const candidate of candidates) {
    if (fs.existsSync(path.join(candidate, 'cordis/lib/index.js'))) return candidate;
  }
  return undefined;
}

/**
 * Loader parity: the plugin must mount through a real Cordis `Context` exactly
 * the way the profile loader mounts it (`ctx.plugin(moduleNamespace, config)`),
 * otherwise a syntax-clean plugin can still take the whole Host down at boot.
 *
 * This test reproduces that path — real Context, real persistence service, real
 * plugin export — and asserts the observable contract: the boot pass runs, the
 * status document is published, and disposal is clean.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const HARNESS = locateHarness();
const skip = HARNESS === undefined ? 'Harness packages not installed on this machine' : false;

/** @type {any} */
let Context;
/** @type {any} */
let JsonlBackend;
if (HARNESS !== undefined) {
  Context = (await import(path.join(HARNESS, 'cordis/lib/index.js'))).Context;
  JsonlBackend = (await import(path.join(HARNESS, 'dsh-session-persistence-jsonl/lib/index.js'))).default;
}

const plugin = await import('../plugins/dsh-session-keeper/index.js');

/** Poll until `predicate` is true or the deadline passes. */
async function until(predicate, timeoutMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() >= deadline) return undefined;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

/** @param {string} file */
function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return undefined;
  }
}

test('the plugin mounts through the loader path and publishes a done status', { skip }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'keeper-wire-'));
  const stateDir = path.join(root, 'state');
  const ctx = new Context();
  // The Host mounts the backend as a service; doing it directly is the same
  // registration the profile's persistence row performs.
  new JsonlBackend(ctx, { root: path.join(root, 'sessions') });
  assert.equal(typeof ctx.get('sessionPersistence'), 'object');

  const fiber = ctx.plugin(plugin, { settleMs: 0, requireProxy: false, stateDir, dryRun: true });
  const status = await until(() => {
    const document = readJson(path.join(stateDir, 'status.json'));
    return document?.summary?.phase === 'done' ? document : undefined;
  });
  assert.ok(status, 'the boot pass must complete and publish status.json');
  assert.equal(status.pid, process.pid);
  assert.equal(status.summary.scanned, 0);
  assert.equal(status.summary.dryRun, true);
  assert.equal(status.config.enabled, true);

  const log = fs.readFileSync(path.join(stateDir, 'keeper.log'), 'utf8');
  assert.match(log, /armed; scanning/);
  assert.match(log, /scan complete: 0 sessions, 0 interrupted/);

  await fiber.dispose();
});

test('the plugin stays dormant when disabled and never touches the store', { skip }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'keeper-wire-off-'));
  const stateDir = path.join(root, 'state');
  const ctx = new Context();
  new JsonlBackend(ctx, { root: path.join(root, 'sessions') });

  const fiber = ctx.plugin(plugin, { enabled: false, settleMs: 0, requireProxy: false, stateDir });
  const status = await until(() => {
    const document = readJson(path.join(stateDir, 'status.json'));
    return document?.summary?.phase === 'disabled' ? document : undefined;
  });
  assert.ok(status, 'a disabled plugin reports itself instead of scanning');
  assert.equal(status.summary.scanned, 0);
  await fiber.dispose();
});

test('the plugin waits for persistence instead of failing on a minimal Host', { skip }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'keeper-wire-late-'));
  const stateDir = path.join(root, 'state');
  const ctx = new Context();
  // Mount the plugin before any persistence service exists: the inject callback
  // must stay pending, and mounting storage later must start the engine.
  const fiber = ctx.plugin(plugin, { settleMs: 0, requireProxy: false, stateDir, dryRun: true });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(fs.existsSync(path.join(stateDir, 'status.json')), false, 'no status before storage exists');

  new JsonlBackend(ctx, { root: path.join(root, 'sessions') });
  const status = await until(() => {
    const document = readJson(path.join(stateDir, 'status.json'));
    return document?.summary?.phase === 'done' ? document : undefined;
  });
  assert.ok(status, 'mounting persistence must activate the engine');
  await fiber.dispose();
});

/** Find the Harness installation's node_modules, if present. */
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

/**
 * Configuration tests: precedence, deep merge, and the derived values that must
 * stay consistent after an override (a stale health URL would gate boot on the
 * wrong port).
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { defaults, loadConfig, merge, toInstallDocument } from '../lib/config.mjs';

test('merge replaces arrays and merges objects deeply', () => {
  const base = { a: 1, nested: { x: 1, y: 2 }, list: [1, 2] };
  const merged = merge(base, { nested: { y: 3 }, list: [9] });
  assert.deepEqual(merged, { a: 1, nested: { x: 1, y: 3 }, list: [9] });
  assert.deepEqual(base, { a: 1, nested: { x: 1, y: 2 }, list: [1, 2] }, 'input must not be mutated');
});

test('defaults are internally consistent', () => {
  const config = defaults({ HOME: '/home/ai' });
  assert.equal(config.proxy.healthUrl, 'http://127.0.0.1:8901/healthz');
  assert.equal(config.harness.port, 30141);
  assert.equal(config.harness.healthUrl, 'http://127.0.0.1:30141/');
  assert.ok(config.log.file.endsWith('stack.log'));
  assert.ok(config.stateDir.includes('dsh-stack'));
  assert.equal(config.keeper.statusFile, '/home/ai/.dsh/session-keeper/status.json');
});

test('a config file overrides defaults without losing sibling fields', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-stack-config-'));
  const file = path.join(dir, 'config.json');
  fs.writeFileSync(
    file,
    JSON.stringify({
      harness: { port: 40000 },
      proxy: { readyTimeoutMs: 1_000 },
      health: { intervalMs: 30_000 },
    }),
  );
  const config = loadConfig({ env: { HOME: '/home/ai' }, file });
  assert.equal(config.harness.port, 40000);
  assert.equal(config.harness.healthUrl, 'http://127.0.0.1:40000/', 'health URL follows the port');
  assert.equal(config.harness.readyTimeoutMs, 300_000, 'untouched field keeps its default');
  assert.equal(config.proxy.readyTimeoutMs, 1_000);
  assert.equal(config.proxy.healthUrl, 'http://127.0.0.1:8901/healthz', 'sibling kept');
  assert.equal(config.health.intervalMs, 30_000);
});

test('environment overrides win and stay consistent', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-stack-config-'));
  const file = path.join(dir, 'missing.json');
  const config = loadConfig({
    env: {
      HOME: '/home/ai',
      DSH_STACK_PORT: '5555',
      DSH_STACK_STATE_DIR: '/tmp/stack-state',
      DSH_STACK_NO_TUNNEL: '1',
      DSH_STACK_PROXY_MODE: 'external',
    },
    file,
  });
  assert.equal(config.harness.port, 5555);
  assert.equal(config.harness.healthUrl, 'http://127.0.0.1:5555/');
  assert.equal(config.stateDir, '/tmp/stack-state');
  assert.equal(config.tunnel.mode, 'off');
  assert.equal(config.proxy.mode, 'external');
});

test('the tunnel target follows the harness port', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-stack-config-'));
  const file = path.join(dir, 'config.json');
  fs.writeFileSync(file, JSON.stringify({ harness: { port: 41234 } }));
  const config = loadConfig({ env: { HOME: '/home/ai' }, file });
  assert.ok(config.tunnel.command.some((value) => value === 'http://127.0.0.1:41234'), config.tunnel.command.join(' '));
});

test('a corrupt config degrades to defaults instead of preventing boot', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-stack-config-'));
  const file = path.join(dir, 'config.json');
  fs.writeFileSync(file, '{ nope');
  const config = loadConfig({ env: { HOME: '/home/ai' }, file });
  assert.equal(config.harness.port, 30141);
  assert.equal(config.configFile, file);
});

test('the install document is valid JSON and drops nothing needed at boot', () => {
  const config = defaults({ HOME: '/home/ai' });
  const document = toInstallDocument(config);
  const round = JSON.parse(JSON.stringify(document));
  assert.equal(round.harness.port, 30141);
  assert.equal(round.proxy.command[0].includes('zen-relay'), true);
  assert.equal(typeof round.log.file, 'string');
  assert.equal(JSON.stringify(round).includes('undefined'), false);
});

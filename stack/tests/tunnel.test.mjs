/**
 * Tunnel tests: a quick tunnel gets a new hostname at every boot, a named
 * tunnel keeps one — and the Harness has to trust exactly that host.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { defaults } from '../lib/config.mjs';

test('the default tunnel is a quick tunnel with no named hostname', () => {
  const config = defaults({ HOME: '/home/ai' });
  assert.equal(config.tunnel.mode, 'own');
  assert.deepEqual(config.tunnel.namedCommand, []);
  assert.equal(config.tunnel.hostname, '');
});

test('a named tunnel carries its own command and hostname', () => {
  const config = defaults({ HOME: '/home/ai' });
  config.tunnel.mode = 'named';
  config.tunnel.namedCommand = ['/usr/bin/cloudflared', 'tunnel', 'run', 'zen-harness'];
  config.tunnel.hostname = 'harness.example.com';

  // The supervisor runs the named command as-is: a quick-tunnel flag such as
  // `--url` would create a second, throwaway hostname.
  assert.ok(!config.tunnel.namedCommand.includes('--url'));
  assert.equal(config.tunnel.namedCommand.at(-1), 'zen-harness');
  assert.equal(config.tunnel.hostname, 'harness.example.com');
});

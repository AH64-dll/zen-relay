/**
 * Bridge tests: the conversation mapping is what decides whether the Harness
 * gets a continuous conversation or a fresh one on every turn, so it is
 * pinned here rather than discovered in production.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  conversationKey,
  errorBody,
  loadConfig,
  messageToText,
  pendingUserText,
} from '../bridge/opencode-bridge.mjs';
import { defaults } from '../lib/config.mjs';

const MODEL = 'deepseek-v4.1-flash-free';

test('a conversation key is stable for the same opening message', () => {
  const messages = [
    { role: 'system', content: 'you are a coding agent' },
    { role: 'user', content: 'hello there' },
  ];
  const first = conversationKey(MODEL, messages);
  const second = conversationKey(MODEL, [...messages]);
  assert.equal(first, second);
});

test('a different opening message is a different conversation', () => {
  const a = conversationKey(MODEL, [{ role: 'user', content: 'hello there' }]);
  const b = conversationKey(MODEL, [{ role: 'user', content: 'hello you' }]);
  assert.notEqual(a, b);
});

test('a different model is a different conversation', () => {
  const messages = [{ role: 'user', content: 'hello' }];
  assert.notEqual(conversationKey(MODEL, messages), conversationKey('beam-free', messages));
});

test('a system prompt change is a different conversation', () => {
  const user = [{ role: 'user', content: 'hello' }];
  const withSystem = [{ role: 'system', content: 'be brief' }, ...user];
  assert.notEqual(conversationKey(MODEL, user), conversationKey(MODEL, withSystem));
});

test('message content is flattened from both string and array parts', () => {
  assert.equal(messageToText({ role: 'user', content: 'plain' }), 'plain');
  assert.equal(
    messageToText({ role: 'user', content: [{ type: 'text', text: 'first' }, { type: 'text', text: 'second' }] }),
    'first\nsecond',
  );
  assert.equal(messageToText({ role: 'user', content: [] }), '');
  assert.equal(messageToText({ role: 'user' }), '');
});

test('only new user turns are forwarded to OpenCode', () => {
  const messages = [
    { role: 'system', content: 'system' },
    { role: 'user', content: 'first question' },
    { role: 'assistant', content: 'first answer' },
    { role: 'user', content: 'second question' },
  ];
  // Nothing sent yet: the first user turn goes out, the system prompt does not.
  assert.equal(pendingUserText(messages, 0), 'first question\n\nsecond question');
  // The turn was already delivered: only what is new is sent again.
  assert.equal(pendingUserText(messages, 3), 'second question');
  assert.equal(pendingUserText(messages, 4), '');
});

test('the bridge accepts the relay key so one secret covers both routes', () => {
  const config = loadConfig({ BRIDGE_API_KEY: 'one,two', OMP_ZEN_RELAY_KEY: 'three', HOME: '/home/ai' });
  assert.deepEqual(config.apiKeys, ['one', 'two', 'three']);
});

test('without keys the bridge falls back to loopback callers only', () => {
  const config = loadConfig({ HOME: '/home/ai' });
  assert.deepEqual(config.apiKeys, []);
});

test('errors use the OpenAI error shape', () => {
  const body = errorBody('nope', 'authentication_error');
  assert.equal(body.error.message, 'nope');
  assert.equal(body.error.type, 'authentication_error');
});

test('the default config carries a bridge that is off until it is configured', () => {
  const config = defaults({ HOME: '/home/ai' });
  assert.equal(config.bridge.mode, 'off');
  assert.equal(config.bridge.healthUrl, 'http://127.0.0.1:8902/healthz');
  const stackDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  assert.equal(config.bridge.command[1], path.join(stackDir, 'bridge', 'opencode-bridge.mjs'));
});

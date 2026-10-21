/**
 * Configuration for the `dsh-stack` supervisor.
 *
 * Precedence, lowest to highest:
 *   1. {@link defaults} computed from the environment the stack was installed in;
 *   2. the JSON document at `$DSH_STACK_CONFIG` (default `~/.config/dsh-stack/config.json`);
 *   3. `DSH_STACK_*` environment variables, for one-off overrides and tests.
 *
 * Every field is optional; a missing field keeps the computed default so an
 * upgraded stack keeps booting with an older config file.
 *
 * @module dsh-stack/config
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Resolve an executable through `PATH` (used to pin absolute paths in config). */
function findExecutable(name, env) {
  const search = env.PATH ?? '';
  for (const dir of search.split(':')) {
    if (dir.length === 0) continue;
    const candidate = path.join(dir, name);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      /* keep looking */
    }
  }
  return undefined;
}

/** Home-relative defaults, computed once per process. */
export function defaults(env = process.env) {
  const home = env.HOME && env.HOME.length > 0 ? env.HOME : os.homedir();
  const nodeBin = path.dirname(process.execPath);
  const relayDefault = path.join(home, '.omp', 'zen-router', 'rust', 'target', 'release', 'zen-relay');
  // The checkout layout is `zen-relay/zen-router/bin/zen-relay`; stack/ lives
  // next to it. Try the shipped binary first, then a source build, then PATH.
  const repoRoot = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
  const checkoutRelayDir = path.join(repoRoot, 'zen-router');
  // Running from a checkout means the relay, its settings and its pool state
  // live in that checkout. Only a bare install without one falls back to the
  // historical ~/.omp location.
  const relayDir = fs.existsSync(checkoutRelayDir) ? checkoutRelayDir : path.join(home, '.omp', 'zen-router');
  const relayCandidates = [
    path.join(repoRoot, 'zen-router', 'bin', 'zen-relay'),
    path.join(repoRoot, 'zen-router', 'rust', 'target', 'release', 'zen-relay'),
    relayDefault,
  ];
  const relayBin = relayCandidates.find((candidate) => fs.existsSync(candidate))
    ?? findExecutable('zen-relay', env)
    ?? relayDefault;
  const cloudflaredBin = findExecutable('cloudflared', env) ?? '/usr/local/bin/cloudflared';
  const dshBin = findExecutable('dsh', env) ?? path.join(nodeBin, 'dsh');
  const stateDir = env.DSH_STACK_STATE_DIR && env.DSH_STACK_STATE_DIR.length > 0
    ? env.DSH_STACK_STATE_DIR
    : path.join(env.XDG_STATE_HOME || path.join(home, '.local', 'state'), 'dsh-stack');
  const configFile = env.DSH_STACK_CONFIG && env.DSH_STACK_CONFIG.length > 0
    ? env.DSH_STACK_CONFIG
    : path.join(env.XDG_CONFIG_HOME || path.join(home, '.config'), 'dsh-stack', 'config.json');

  return {
    /** Human label used in logs and status. */
    name: 'dsh-stack',
    /** Absolute path to this installation, filled in by the installer. */
    installDir: path.dirname(path.dirname(fileURLToPath(import.meta.url))),
    configFile,
    stateDir,
    /** The dsh home the Harness uses (sessions, profiles, credentials). */
    dshHome: env.DSH_HOME && env.DSH_HOME.length > 0 ? env.DSH_HOME : path.join(home, '.dsh'),
    /** Where the sign-in URL is published (compatibility with dsh-supervisor.sh). */
    urlFile: path.join(home, 'dsh-url.txt'),

    network: {
      /** Gate startup on a real outbound connection instead of trusting `network-online.target`. */
      enabled: true,
      host: 'opencode.ai',
      port: 443,
      timeoutMs: 120_000,
      intervalMs: 2_000,
      /** Start anyway when the network never answers (offline use still gets a local UI). */
      failOpen: true,
    },

    proxy: {
      /** `own` spawns and supervises the process here; `external` only probes it. */
      mode: 'own',
      command: [relayBin],
      cwd: relayDir,
      env: {
        PORT: '8901',
        SETTINGS_FILE: path.join(relayDir, 'settings.json'),
        LANES_FILE: path.join(relayDir, 'lanes.json'),
        BURNED_FILE: path.join(relayDir, 'burned.json'),
      },
      healthUrl: 'http://127.0.0.1:8901/healthz',
      /** A `2xx` body containing `"ok":true` counts as ready. */
      expectJsonOk: true,
      readyTimeoutMs: 180_000,
      required: true,
    },

    tunnel: {
      /** `own` runs cloudflared; `external` leaves it to another unit; `off` disables it. */
      mode: 'own',
      command: [cloudflaredBin, 'tunnel', '--url', 'http://127.0.0.1:30141', '--no-autoupdate'],
      /** Restart the Harness with `--trusted-host` when the quick-tunnel hostname appears. */
      publishUrl: true,
      readyTimeoutMs: 90_000,
      /** Never block a local boot on a third-party tunnel. */
      required: false,
    },

    harness: {
      mode: 'own',
      /** argv appended after the dsh binary; `--port`/`--no-open` are added automatically. */
      command: dshBin,
      cwd: path.join(home),
      port: 30141,
      extraArgs: [],
      env: {},
      healthUrl: 'http://127.0.0.1:30141/',
      readyTimeoutMs: 300_000,
      required: true,
    },

    health: {
      intervalMs: 15_000,
      timeoutMs: 5_000,
      /** Consecutive failed probes before the component is restarted. */
      failureThreshold: 3,
      /** Stop pinging the systemd watchdog after this long unhealthy (forces a hard restart). */
      escalateAfterMs: 180_000,
      watchdog: true,
    },

    restart: {
      initialBackoffMs: 1_000,
      maxBackoffMs: 60_000,
      /** A component healthy this long resets its backoff. */
      resetAfterMs: 120_000,
      /** Boot-time grace before the same component may be restarted again. */
      minRestartIntervalMs: 5_000,
      graceMs: 15_000,
      /** Give up (exit non-zero, let systemd decide) after this many restarts in the window. */
      maxRestartsPerWindow: 12,
      windowMs: 300_000,
    },

    log: {
      file: path.join(stateDir, 'stack.log'),
      maxBytes: 8 * 1024 * 1024,
      keep: 5,
      /** Echo warnings and lifecycle lines to stderr (journald). */
      echoWarnings: true,
    },

    /** Paths owned by the plugin; read-only for the daemon. */
    keeper: {
      statusFile: path.join(env.DSH_HOME && env.DSH_HOME.length > 0 ? env.DSH_HOME : path.join(home, '.dsh'), 'session-keeper', 'status.json'),
      /** Run one post-boot session scan to report how many interrupts remain. */
      verifyScan: true,
      verifyScanDelayMs: 45_000,
    },
  };
}

/**
 * Deep-merge `overlay` onto `base`, replacing arrays wholesale.
 * @param {any} base
 * @param {any} overlay
 * @returns {any}
 */
export function merge(base, overlay) {
  if (overlay === undefined || overlay === null) return base;
  if (Array.isArray(overlay)) return overlay.slice();
  if (typeof overlay !== 'object') return overlay;
  if (typeof base !== 'object' || base === null || Array.isArray(base)) return { ...overlay };
  const out = { ...base };
  for (const [key, value] of Object.entries(overlay)) {
    out[key] = merge(base[key], value);
  }
  return out;
}

/**
 * Load the effective configuration.
 * @param {{env?: NodeJS.ProcessEnv, file?: string}} [options]
 * @returns {ReturnType<typeof defaults> & Record<string, any>}
 */
export function loadConfig(options = {}) {
  const env = options.env ?? process.env;
  let config = defaults(env);
  const file = options.file ?? config.configFile;
  let fromFile = {};
  try {
    const raw = fs.readFileSync(file, 'utf8');
    const parsed = JSON.parse(raw);
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) fromFile = parsed;
  } catch (error) {
    const code = /** @type {any} */ (error)?.code;
    if (code !== 'ENOENT') {
      // A corrupt config must not prevent the machine from booting; fall back
      // to defaults and say so on stderr (logged by the caller).
      process.stderr.write(`dsh-stack: config ${file} is unreadable (${code ?? /** @type {Error} */ (error).message}); using defaults\n`);
    }
  }
  config = merge(config, fromFile);
  config.configFile = file;

  if (env.DSH_STACK_STATE_DIR) config.stateDir = env.DSH_STACK_STATE_DIR;
  if (env.DSH_STACK_URL_FILE) config.urlFile = env.DSH_STACK_URL_FILE;
  if (env.DSH_STACK_PORT) config.harness.port = Number(env.DSH_STACK_PORT) || config.harness.port;
  if (env.DSH_STACK_PROXY_URL) config.proxy.healthUrl = env.DSH_STACK_PROXY_URL;
  if (env.DSH_STACK_PROXY_MODE) config.proxy.mode = env.DSH_STACK_PROXY_MODE;
  if (env.DSH_STACK_NO_TUNNEL === '1') config.tunnel.mode = 'off';
  if (env.DSH_STACK_DSH_BIN) config.harness.command = env.DSH_STACK_DSH_BIN;
  if (env.DSH_STACK_VERBOSE === '1') config.log.echoWarnings = true;

  // Values derived from other values: keep them consistent after an override.
  if (!config.log.file) config.log.file = path.join(config.stateDir, 'stack.log');
  if (config.harness.mode === 'own') {
    config.harness.healthUrl = config.harness.healthUrl && config.harness.healthUrl.length > 0
      ? config.harness.healthUrl
      : `http://127.0.0.1:${config.harness.port}/`;
    const url = new URL(config.harness.healthUrl);
    url.port = String(config.harness.port);
    config.harness.healthUrl = url.toString();
  }
  if (Array.isArray(config.tunnel.command)) {
    const url = config.tunnel.command.findIndex((value) => value.startsWith('http://127.0.0.1'));
    if (url >= 0) config.tunnel.command[url] = `http://127.0.0.1:${config.harness.port}`;
  }
  return config;
}

/**
 * The JSON document the installer writes. It is the effective config with the
 * volatile fields removed, so the file stays readable and diffable.
 * @param {Record<string, any>} config
 */
export function toInstallDocument(config) {
  return {
    $comment: 'dsh-stack supervisor configuration. Edit freely; `dsh-stack doctor` validates it.',
    name: config.name,
    installDir: config.installDir,
    stateDir: config.stateDir,
    dshHome: config.dshHome,
    urlFile: config.urlFile,
    network: config.network,
    proxy: config.proxy,
    tunnel: config.tunnel,
    harness: config.harness,
    health: config.health,
    restart: config.restart,
    log: { ...config.log, file: path.join(config.stateDir, 'stack.log') },
    keeper: config.keeper,
  };
}

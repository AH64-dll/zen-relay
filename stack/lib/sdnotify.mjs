/**
 * sd_notify(3) bridge.
 *
 * `dsh-stack` runs as a `Type=notify` unit, so systemd only considers the
 * machine's harness "started" once READY has been sent — which this process
 * sends only after the proxy and the Harness both answer their readiness
 * probes. It also pings the watchdog while everything is healthy, so a wedged
 * supervisor (not just a crashed one) is restarted by systemd.
 *
 * Node has no `unix_dgram` socket API, so notifications go through the
 * `systemd-notify` helper, which inherits `NOTIFY_SOCKET` from our environment.
 * The unit therefore needs `NotifyAccess=all` (the helper is a child process).
 *
 * Outside systemd every call is a no-op, which keeps `dsh-stack run` usable
 * from a shell for testing.
 *
 * @module dsh-stack/sdnotify
 */

import { execFileSync } from 'node:child_process';

/**
 * @param {{env?: NodeJS.ProcessEnv, logger?: {warn: Function, info?: Function}, exec?: typeof execFileSync}} [options]
 */
export function createNotifier(options = {}) {
  const env = options.env ?? process.env;
  const logger = options.logger;
  const exec = options.exec ?? execFileSync;
  const socket = env.NOTIFY_SOCKET;
  const enabled = typeof socket === 'string' && socket.length > 0;
  let failures = 0;
  let disabledByFailure = false;

  /**
   * @param {string[]} args
   * @returns {boolean} whether the notification was delivered
   */
  function send(args) {
    if (!enabled || disabledByFailure) return false;
    try {
      exec('systemd-notify', args, {
        timeout: 3_000,
        stdio: 'ignore',
        env: { ...env, NOTIFY_SOCKET: /** @type {string} */ (socket) },
      });
      failures = 0;
      return true;
    } catch (error) {
      failures += 1;
      if (failures >= 5) {
        disabledByFailure = true;
        logger?.warn('sdnotify disabled after repeated failures', {
          detail: /** @type {Error} */ (error).message.split('\n')[0],
        });
      }
      return false;
    }
  }

  return {
    enabled,
    /** Signal successful startup; systemd stops waiting for the unit. */
    ready: (status) => send(status === undefined ? ['--ready'] : ['--ready', `STATUS=${status}`]),
    /** Keepalive; systemd restarts the unit when this stops for WatchdogSec. */
    watchdog: () => send(['WATCHDOG=1']),
    /** Free-form status line shown by `systemctl status`. */
    status: (text) => send([`STATUS=${text}`]),
    /** Shutdown started. */
    stopping: (text) => send(text === undefined ? ['STOPPING=1'] : ['STOPPING=1', `STATUS=${text}`]),
    /** Configuration/units reloaded. */
    reloading: () => send(['RELOADING=1']),
    /** True when notifications are not reaching systemd. */
    degraded: () => (enabled ? failures > 0 : false),
    socket,
  };
}

/**
 * @local/dsh-session-keeper — crash recovery and keep-alive for active
 * Harness sessions.
 *
 * What it does
 * ------------
 * The Harness already makes a session log durable after every batch, and a
 * crashed writer leaves its final turn open on disk. This plugin turns that
 * fact into automatic recovery:
 *
 *   1. On Host start (after the model route is verified ready) it lists every
 *      stored session and reads the tail of each log.
 *   2. A log whose last `turn/start` has no later `turn/end` was interrupted by
 *      a crash, power loss, or kill. It is resumed exactly once through the
 *      public `agents.resume()` path, which claims the single-writer lease and
 *      appends the synthetic closers that balance the log.
 *   3. The interrupted request is reconstructed from the log and re-issued as
 *      a recovery prompt that tells the model to re-establish ground truth
 *      before continuing, never to replay finished work.
 *   4. A durable ledger bounds attempts per turn and per session, so a session
 *      that keeps killing the Host is quarantined instead of looping forever.
 *
 * Safety properties
 * -----------------
 * - Sessions live in this process are skipped; live sessions in another
 *   process are protected by the persistence write lease (`SessionAlreadyOwnedError`).
 * - Subagent children are skipped by default: their parent owns them.
 * - Recovery waits for the model proxy `/healthz` gate, so it never burns an
 *   attempt on a dead route.
 * - Every decision is written to `$DSH_HOME/session-keeper/status.json`, which
 *   the `dsh-stack` service reads and republishes in its own status document.
 *
 * Manual control
 * --------------
 *   /keeper status    what the last pass saw and decided
 *   /keeper scan      dry run: list what would be resumed, change nothing
 *   /keeper recover   run a pass now
 */

import { SessionKeeper } from './lib/keeper.mjs';

export const name = 'dsh-session-keeper';

/**
 * `sessionPersistence` is required for the engine to do anything; the other
 * services it uses (`agents`, `agentPresets`, `agentDefaultModel`) are read
 * through `ctx.get()` at use time so the plugin mounts in minimal Hosts too.
 * The Host plugin export form takes no hard `inject` here: the recovery pass is
 * deferred until persistence is available.
 */
export function apply(ctx, config) {
  const keeper = new SessionKeeper(ctx, config);

  // Start only once durable storage exists; in a Host without persistence the
  // engine stays dormant rather than reporting phantom recoveries.
  ctx.inject(['sessionPersistence'], (persistenceCtx) => {
    persistenceCtx.effect(() => keeper.start());
  });

  // Manual control surface. Optional because headless hosts may not mount a
  // command registry.
  ctx.inject(['commands'], (commandCtx) => {
    commandCtx.effect(() =>
      commandCtx.commands.register({
        definitionId: '@local/dsh-session-keeper',
        name: 'keeper',
        description: 'Crash recovery for interrupted sessions: status | scan | recover',
        input: { hint: '[status|scan|recover]' },
        recordInput: false,
        handler: ({ rawInput }) => {
          const action = String(rawInput ?? '').trim().toLowerCase();
          const summary = keeper.summary;

          if (action === 'scan' || action === 'dry-run' || action === 'dryrun') {
            void keeper.runOnce({ trigger: 'command', dryRun: true });
            return {
              kind: 'success',
              text: 'Scanning every stored session in dry-run mode; nothing will be resumed. Check `/keeper status` in a few seconds.',
            };
          }

          if (action === 'recover' || action === 'run') {
            if (summary.phase === 'resuming' || keeper.running) {
              return { kind: 'success', text: 'A recovery pass is already running.' };
            }
            void keeper.runOnce({ trigger: 'command' });
            return {
              kind: 'success',
              text: `Recovery pass started (dryRun=${keeper.config.dryRun}). Interrupted turns are resumed through the harness's own agent resume path; check \`/keeper status\`.`,
            };
          }

          const resumed = summary.resumed.length;
          const skipped = summary.skipped.length;
          const lines = [
            `session-keeper: phase=${summary.phase}${summary.dryRun ? ' (dry-run)' : ''}`,
            `scanned ${summary.scanned} session(s), ${summary.candidates} interrupted, ${resumed} resumed, ${skipped} skipped`,
            summary.phase === 'idle'
              ? `armed: the boot pass runs ${Math.round(keeper.config.settleMs / 1000)}s after mount; use \`/keeper recover\` to run it now`
              : '',
            summary.finishedAt === undefined ? '' : `last pass finished ${new Date(summary.finishedAt).toISOString()}`,
            summary.errors.length > 0 ? `errors: ${summary.errors.slice(0, 5).join(' | ')}` : '',
            `state: ${keeper.config.statusFile}`,
          ].filter((line) => line.length > 0);
          return { kind: 'success', text: lines.join('\n') };
        },
      }),
    );
  });
}
